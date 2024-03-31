# SPDX-License-Identifier: LGPL-2.1+

import os
from collections.abc import Sequence

from mkosi.config import Architecture, Config
from mkosi.context import Context
from mkosi.distributions import Distribution, DistributionInstaller, PackageType
from mkosi.installer import PackageManager
from mkosi.installer.emerge import Emerge
from mkosi.log import die


class Installer(DistributionInstaller):
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
    def package_manager(cls, config: Config) -> type[PackageManager]:
        return Emerge

    @classmethod
    def setup(cls, context: Context) -> None:
        Emerge.setup(context, filelists=False)

    @classmethod
    def sync(cls, context: Context) -> None:
        Emerge.sync(context, False)

    @classmethod
    def install(cls, context: Context) -> None:
        cls.install_packages(
            context,
            [
                "sys-apps/baselayout",
                "sec-keys/openpgp-keys-gentoo-release"
            ]
        )

    @classmethod
    def install_packages(cls, context: Context, packages: Sequence[str]) -> None:
        Emerge.install(context, packages)

        for d in context.root.glob("usr/src/linux-*"):
            kver = d.name.removeprefix("linux-")
            kimg = d / {
                Architecture.x86_64: "arch/x86/boot/bzImage",
                Architecture.arm64: "arch/arm64/boot/Image.gz",
                Architecture.arm: "arch/arm/boot/zImage",
            }[context.config.architecture]
            vmlinuz = context.root / "usr/lib/modules" / kver / "vmlinuz"
            if not vmlinuz.exists() and not vmlinuz.is_symlink():
                vmlinuz.symlink_to(os.path.relpath(kimg, start=vmlinuz.parent))

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
