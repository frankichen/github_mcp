"""Application package bootstrap."""

from app import mygithub10 as _mygithub10
from app.mygithub10_runtime_fix import install as _install_mygithub10_runtime_fix

_install_mygithub10_runtime_fix(_mygithub10)
