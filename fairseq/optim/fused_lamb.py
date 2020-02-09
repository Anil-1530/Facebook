# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from fairseq.optim import FairseqOptimizer, register_optimizer, optimizer_registry

from typing import Iterable, Tuple


@optimizer_registry.register('lamb')
class FairseqLAMB(FairseqOptimizer):
    """LAMB optimizer."""

    def __init__(self, params, lr: Iterable[float], lamb_betas: Tuple[float, float]=(0.9, 0.999), lamb_eps: float=1e-8,
                 weight_decay: float=0.0):
        super().__init__()
        try:
            from apex.optimizers import FusedLAMB
            self.lr = lr
            self.lamb_betas = lamb_betas
            self.lamb_eps = lamb_eps
            self.weight_decay = weight_decay
            self._optimizer = FusedLAMB(params, **self.optimizer_config)
        except ImportError:
            raise ImportError('Please install apex to use LAMB optimizer')

    @staticmethod
    def add_args(parser):
        """Add optimizer-specific arguments to the parser."""
        # fmt: off
        parser.add_argument('--lamb-betas', default='(0.9, 0.999)', metavar='B',
                            help='betas for LAMB optimizer')
        parser.add_argument('--lamb-eps', type=float, default=1e-8, metavar='D',
                            help='epsilon for LAMB optimizer')
        parser.add_argument('--weight-decay', '--wd', default=0.0, type=float, metavar='WD',
                            help='weight decay')
        # fmt: on

    @property
    def optimizer_config(self):
        """
        Return a kwarg dictionary that will be used to override optimizer
        args stored in checkpoints. This allows us to load a checkpoint and
        resume training using a different set of optimizer args, e.g., with a
        different learning rate.
        """
        return {
            'lr': self.lr[0],
            'betas': self.lamb_betas,
            'eps': self.lamb_eps,
            'weight_decay': self.weight_decay,
        }

    @property
    def supports_flat_params(self):
        return False
