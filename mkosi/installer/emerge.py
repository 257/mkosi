# SPDX-License-Identifier: LGPL-2.1+
import contextlib
import sys
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Optional

from mkosi.config import Config, OutputFormat
from mkosi.context import Context
from mkosi.installer import PackageManager
from mkosi.log import ARG_DEBUG
from mkosi.run import CompletedProcess, apivfs_options, find_binary, run, sandbox_cmd
from mkosi.util import _FILE, PathString, flock


class Emerge(PackageManager):
    installroot: Path

    @classmethod
    def executable(cls, config: Config) -> str:
        return Path(
            config.environment.get("MKOSI_EMERGE")
            or find_binary("emerge", root=config.tools())
            or find_binary("emerge")
            or "emerge"
        ).name

    @classmethod
    def subdir(cls, config: Config) -> Path:
        return Path(".")

    @classmethod
    def package_subdirs(cls, cache: Path) -> list[tuple[Path, Path]]:
        dir(str(cache))
        return [(Path("binpkgs"), Path("binpkgs"))]

    @classmethod
    def scripts(cls, context: Context) -> dict[str, list[PathString]]:
        return {
            "emerge": cls.apivfs_script_cmd(context)
            + cls.env_cmd(context)
            + cls.cmd(context),
            "mkosi-install": ["emerge"],
            "mkosi-upgrade": ["emerge", "--update"],
            "mkosi-remove": ["emerge", "--unmerge"],
            "mkosi-reinstall": ["emerge"],
        }

    # TODO: remove this if it's identical to super
    @classmethod
    def options(cls, *, root: PathString, apivfs: bool = True) -> list[PathString]:
        return [
            *(apivfs_options(root=Path(root)) if apivfs else []),
            "--become-root",
            "--suppress-chown",
            "--suppress-sync",
        ]  # fmt: skip

    @classmethod
    def setenv(cls, context: Context, root: PathString) -> list[PathString]:
        return [
            "--setenv", "PORTAGE_CONFIGROOT", str(root),
            "--setenv", "GPG_VERIFY_USER_DROP", "root",
            "--setenv", "GPG_VERIFY_GROUP_DROP", "root",
            "--setenv", "FEATURES", cls.features(context.config)
        ]  # fmt: skip

    @classmethod
    def mounts(cls, context: Context) -> list[PathString]:
        mounts = []
        root = Path("/")
        if context.config.tools() != root:
            for i in [
                "etc",
                "etc/locale.gen",
                "etc/dispatch-conf.conf",
                "etc/env.d",
                "etc/ssl",
                "opt",
                "var/cache/edb",
                "var/cache/ldconfig",
                "var/db/pkg",
                "var/lib/portage",
            ]:
                mounts += ["--bind", context.config.tools() / i, root / i]
        if context.config.package_cache_dir is not None:
            mounts += ["--bind", context.config.package_cache_dir, "/var/cache/binpkgs"]
        mounts += ["--bind", "/var/db/repos", "/var/db/repos"]
        # `emerge(1)` expects a valid cls.installroot / etc/portage/make.profile
        mounts += ["--ro-bind", "/var/db/repos", cls.installroot / "var/db/repos"]

        CONFIG_ROOT = Path("etc/portage")
        # main place for build customisations is `StandboxTree=`

        if context.sandbox_tree is not None:
            mounts += [*([
                "--bind", context.sandbox_tree / CONFIG_ROOT, cls.installroot / CONFIG_ROOT,
                "--bind", context.sandbox_tree / CONFIG_ROOT, root / CONFIG_ROOT,
                ] if context.sandbox_tree.exists() else [])
            ]

        mounts += [
            # many atoms [soft] expect a configured kernel at
            # /usr/src/linux mount it for everybody
            "--ro-bind", context.config.tools() / "usr/src", cls.installroot / "usr/src",
            "--ro-bind", context.config.tools() / "usr/src", "/usr/src",
            "--ro-bind", context.config.tools() / "lib", "/lib", # for run_depmod()
            # decouple distfiles from builds, this benefits the build farms
            "--bind", "/var/cache/distfiles", "/var/cache/distfiles",
        ]
        mounts += [*(["--bind", "/dev/steve", "/dev/steve",] if Path("/dev/steve").exists() else [])]

        return mounts

    @classmethod
    def setup(cls, context: Context) -> None:
        cls.installroot = Path("/buildroot")
        return

    @classmethod
    def features(cls, config: Config) -> str:
        return " ".join([*(["noman", "nodoc", "noinfo"] if config.with_docs else [])])

    @classmethod
    def finalize_environment(cls, context: Context) -> dict[str, str]:
        return super().finalize_environment(context)

    @classmethod
    def cmd(cls, context: Context, root: Optional[Path] = None) -> list[PathString]:
        return [
            cls.executable(context.config),
            *(
                ["--verbose", "--quiet-fail=n"]
                if ARG_DEBUG.get()
                else ["--quiet-build", "--quiet"]
            ),
            *([f"--root={cls.installroot}"] if root is None else []),
        ]

    @classmethod
    @contextlib.contextmanager
    def sandbox(
        cls,
        context: Context,
        *,
        apivfs: bool,
        options: Sequence[PathString] = (),
    ) -> Generator[list[PathString]]:
        with (
            flock(context.config.package_cache_dir_or_default()),
            sandbox_cmd(
                network=True,
                devices=True,
                tools=context.config.tools(),
                relaxed=False,
                options=[*context.rootoptions(cls.installroot),
                         *cls.mounts(context),
                         *cls.options(root=Path("/"), apivfs=apivfs),
                         *cls.setenv(context, cls.installroot),
                         *options,]
                ) as sandbox
            ):
            yield sandbox

    @classmethod
    def invoke(
        cls,
        context: Context,
        arguments: Sequence[str] = (),
        options: Sequence[PathString] = (),
        *,
        apivfs: bool = False,
        stdout: _FILE = sys.stdout,
    ) -> CompletedProcess:
        if ARG_DEBUG.get():
            run(
                [*cls.cmd(context), "--info"],
                sandbox=cls.sandbox(context, apivfs=apivfs),
                env=context.config.environment,
                stdout=stdout,
            )
        return run(
            cls.cmd(context) + [*options, *arguments],
            sandbox=cls.sandbox(context, apivfs=apivfs),
            env=context.config.environment,
            stdout=stdout,
        )

    @classmethod
    def sync(cls, context: Context, force: bool) -> None:
        if context.config.output_format == OutputFormat.confext:
            return

        # TODO: document 2+1 magic/undocumented things here:
        # 1. mkosi.sandbox/installroot
        # 2. mkosi.sandbox/stage3
        # 3. @mkosi, defined here: mkosi.sandbox/installroot/etc/portage/sets/mkosi
        for f in (["--oneshot", "sys-apps/portage"],
                  ["--update", "--newuse", "--deep", "--changed-use", "--with-bdeps=y", "@world", "@mkosi"],
                  ["@preserved-rebuild"]):
            run([*cls.cmd(context, Path("/")), *f,], sandbox=cls.sandbox(context, apivfs=True),
                env=context.config.environment)

        return

    @classmethod
    def createrepo(cls, context: Context) -> None:
        cls.sync(context, True if context.args.force == 2 else False)

    @classmethod
    def install(
        cls,
        context: Context,
        packages: Sequence[str],
        *,
        apivfs: bool = True,
        allow_downgrade: bool = False,
    ) -> None:
        cls.invoke(context, (), packages, apivfs=apivfs)

    @classmethod
    def remove(
        cls,
        context: Context,
        packages: Sequence[str],
        *,
        apivfs: bool = True,
    ) -> None:
        cls.invoke(
            context, arguments=list(packages), options=["--unmerge"], apivfs=True
        )
