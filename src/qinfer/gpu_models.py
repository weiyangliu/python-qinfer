#!/usr/bin/python
# -*- coding: utf-8 -*-
##
# gpu_models.py: Demonstrates the use of GPU-accelerated likelihood evaluation.
##
# © 2017, Chris Ferrie (csferrie@gmail.com) and
#         Christopher Granade (cgranade@cgranade.com).
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     1. Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#
#     2. Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#
#     3. Neither the name of the copyright holder nor the names of its
#        contributors may be used to endorse or promote products derived from
#        this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
##
"""
This module demonstrates the use of OpenCL to accelerate sequential Monte Carlo
by implementing a simple cosine-likelihood model as an OpenCL kernel. When run
as a script, this module then compares the performance of the OpenCL-accelerated
model to the pure-NumPy model implemented in the QInfer project.
"""

## FEATURES ####################################################################

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

## EXPORTS #####################################################################

__all__ = [
    'AcceleratedPrecessionModel'
]

## IMPORTS #####################################################################

from builtins import range

try:
    import pyopencl as cl
except ImportError:
    cl = None
    import warnings
    warnings.warn(
        "Could not import PyOpenCL. GPU models will not work.",
        ImportWarning
    )

import numpy as np
import numpy.linalg as la
import time

from qinfer.abstract_model import Model, FiniteOutcomeModel
from qinfer.test_models import SimplePrecessionModel
from qinfer.smc import SMCUpdater
from qinfer.distributions import UniformDistribution

## KERNELS #####################################################################

COS_MODEL_KERNEL = """
__kernel void cos_model(
    int n_experiments,
    __global const float *models,
    __global const float *expparams,
    __global float *likelihoods
) {
    // Assuming two-outcome model, and finding Pr(0 | model; expparams).
    int idx_model      = get_global_id(0);
    int idx_experiment = get_global_id(1);
    likelihoods[idx_model * n_experiments + idx_experiment] = pow(cos(
        models[idx_model] * expparams[idx_experiment] / 2
    ), 2);
}
"""

## CLASSES #####################################################################

class AcceleratedPrecessionModel(FiniteOutcomeModel):
    r"""
    Reimplementation of `qinfer.test_models.SimplePrecessionModel`, using OpenCL
    to accelerate computation.
    """
    
    def __init__(self, context=None):
        super(AcceleratedPrecessionModel, self).__init__()

        if cl is None:
            raise ImportError(
                "AcceleratedPrecessionModel requires "
                "GPU acceleration support. Please install PyOpenCL to "
                "use this model."
            )
    
        self._ctx = cl.create_some_context() if context is None else context
        self._queue = cl.CommandQueue(self._ctx)

        
        self._prg = cl.Program(self._ctx, COS_MODEL_KERNEL).build()
        
    
    ## PROPERTIES ##
    
    @property
    def n_modelparams(self):
        return 1
        
    @property
    def expparams_dtype(self):
        return 'float'
    
    @property
    def is_n_outcomes_constant(self):
        """
        Returns ``True`` if and only if the number of outcomes for each
        experiment is independent of the experiment being performed.
        
        This property is assumed by inference engines to be constant for
        the lifetime of a Model instance.
        """
        return True
    
    ## METHODS ##
    
    @staticmethod
    def are_models_valid(modelparams):
        return np.all(
            modelparams > 0,
            axis=1
        )
    
    def n_outcomes(self, expparams):
        """
        Returns an array of dtype ``uint`` describing the number of outcomes
        for each experiment specified by ``expparams``.
        
        :param numpy.ndarray expparams: Array of experimental parameters. This
            array must be of dtype agreeing with the ``expparams_dtype``
            property.
        """
        return 2

    
    def likelihood(self, outcomes, modelparams, expparams):
        # By calling the superclass implementation, we can consolidate
        # call counting there.
        super(AcceleratedPrecessionModel, self).likelihood(outcomes, modelparams, expparams)
        
        # Possibly add a second axis to modelparams.
        if len(modelparams.shape) == 1:
            modelparams = modelparams[..., np.newaxis]
        
        # Convert to float32 if needed.
        mps = modelparams.astype(np.float32)
        eps = expparams.astype(np.float32)

        # Allocating a buffer for the pr0 returns.
        pr0 = np.empty((mps.shape[0], eps.shape[0]), dtype=mps.dtype)

        # Move buffers to the GPU.
        mf = cl.mem_flags
        
        mps_buf = cl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=mps)
        eps_buf = cl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=eps)
        dest_buf = cl.Buffer(self._ctx, mf.WRITE_ONLY, pr0.nbytes)

        # Run the kernel with global worksize (n_models, n_experiments).
        self._prg.cos_model(self._queue, pr0.shape, None, np.int32(eps.shape[0]), mps_buf, eps_buf, dest_buf)

        # Copy the buffer back from the GPU and free memory there.
        cl.enqueue_copy(self._queue, pr0, dest_buf)
        mps_buf.release()
        eps_buf.release()
        dest_buf.release()
        
        # Now we concatenate over outcomes.
        return FiniteOutcomeModel.pr0_to_likelihood_array(outcomes, pr0)

## SCRIPT ######################################################################

if __name__ == "__main__":
    # NOTE: This is now redundant with the perf_testing module.

    simple_model = SimplePrecessionModel()

    for model in [AcceleratedPrecessionModel(), SimplePrecessionModel()]:
        
        true = np.random.random(1)
        updater = SMCUpdater(model, 100000, UniformDistribution([0, 1]))
        
        tic = time.time()
        
        for idx_exp in range(200):
            if not (idx_exp % 20):
                print(idx_exp)
            expparams = np.array([(9 / 8) ** idx_exp])
            updater.update(simple_model.simulate_experiment(true, expparams), expparams)
            
        print(model, updater.est_mean(), true, time.time() - tic)
