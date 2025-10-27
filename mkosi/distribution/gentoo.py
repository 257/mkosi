# SPDX-License-Identifier: LGPL-2.1+

import os
from collections.abc import Sequence

from mkosi.config import Architecture, Config, OutputFormat
from mkosi.context import Context
from mkosi.distribution import Distribution, DistributionInstaller, PackageType
from mkosi.installer import PackageManager
from mkosi.installer.emerge import Emerge
from mkosi.log import die


class Installer(DistributionInstaller, distribution=Distribution.gentoo):
    @classmethod
    def pretty_name(cls) -> str:
        return "Gentoo"

    @classmethod
    def filesystem(cls) -> str:
        return "btrfs"

    @classmethod
    def package_type(cls) -> PackageType:
        return PackageType.ebuild

    @classmethod
    def default_release(cls) -> str:
        return "23.0"

    @classmethod
    def default_tools_tree_distribution(cls) -> Distribution:
        return Distribution.gentoo

    @classmethod
    def package_manager(cls, config: Config) -> type[Emerge]:
        return Emerge

    @classmethod
    def setup(cls, context: Context) -> None:
        Emerge.setup(context)

    @classmethod
    def sync(cls, context: Context) -> None:
        Emerge.sync(context, False)

    @classmethod
    def install(cls, context: Context) -> None:
        if context.config.output_format == OutputFormat.confext:
            return
        cls.install_packages(context, [ "sys-apps/baselayout" ])

    @classmethod
    def architecture(cls, arch: Architecture) -> str:
        a = {
            Architecture.x86_64: "amd64",
            Architecture.arm64: "arm64",
            Architecture.arm: "arm",
        }.get(arch)

        if not a:
            die(f"Architecture {a} is not supported by ${cls.pretty_name()}")

        return a
