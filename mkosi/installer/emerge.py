# SPDX-License-Identifier: LGPL-2.1+
import logging
import sys
import textwrap
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from mkosi.config import Config
from mkosi.context import Context
from mkosi.installer import PackageManager
from mkosi.log import ARG_DEBUG
from mkosi.run import CompletedProcess, apivfs_options, find_binary, run, sandbox_cmd
from mkosi.util import _FILE, PathString


class Emerge(PackageManager):
    installroot: Path

    @classmethod
    def executable(cls, config: Config) -> str:
        return Path(
            config.environment.get("MKOSI_EMERGE") or
            find_binary("emerge", root=config.tools()) or
            find_binary("emerge") or
            "emerge"
        ).name

    @classmethod
    def subdir(cls, config: Config) -> Path:
        return Path("portage")

    @classmethod
    def scripts(cls, context: Context) -> dict[str, list[PathString]]:
        return {
            "emerge": cls.apivfs_script_cmd(context) + cls.env_cmd(context) + cls.cmd(context),
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
        if context.config.tools() is not None:
            mounts += [
                "--bind", context.config.tools() / "etc/dispatch-conf.conf", "/etc/dispatch-conf.conf",
                "--bind", context.config.tools() / "etc/env.d", "/etc/env.d",
                "--bind", context.config.tools() / "etc/shadow", "/etc/shadow",
                "--bind", context.config.tools() / "etc/gshadow", "/etc/gshadow",
                "--bind", context.config.tools() / "etc/passwd", "/etc/passwd",
                "--bind", context.config.tools() / "etc/group", "/etc/group",
                "--bind", context.config.tools() / "etc/ssl", "/etc/ssl",

                "--bind", context.config.tools() / "var/cache/edb", "/var/cache/edb",
                "--bind", context.config.tools() / "var/cache/ldconfig", "/var/cache/ldconfig",
                "--bind", context.config.tools() / "var/cache/repos", "/var/cache/repos",

                "--bind", context.config.tools() / "var/db/pkg", "/var/db/pkg",
                "--bind", context.config.tools() / "var/db/repos", "/var/db/repos",

                "--bind", context.config.tools() / "var/lib/portage", "/var/lib/portage",

                "--bind", context.config.tools() / "var/log/emerge.log", "/var/log/emerge.log",

                "--bind", context.config.tools() / "opt", "/opt",
            ]
        else:
            # super calls finalize_passwd_symlinks() but ro-binds )
            # some build dependencies, however, need to write to user/group database
            # final image, installroot, is not affected by this
            mounts += [ *super().mounts(context) ]

        if context.config.package_cache_dir is not None:
            # TODO: try not to mix our binaries with host's
            binpkgs_dir = context.config.package_cache_dir / "binpkgs"
            if not binpkgs_dir.exists():
                binpkgs_dir.mkdir(parents=True, exist_ok=True)
            mounts += ["--bind", binpkgs_dir, "/var/cache/binpkgs"]
            # repo clones can be expensive, symlink to users existing repos
            repos_dir = context.config.package_cache_dir / "var/db/repos"
            if not repos_dir.exists():
                repos_dir.mkdir(parents=True, exist_ok=True)
            mounts += ["--bind", repos_dir, "/var/db/repos"]
            # `emerge(1)` expects a valid cls.installroot / etc/portage/make.profile
            mounts += ["--ro-bind", repos_dir, cls.installroot / "var/db/repos"]

        CONFIG_ROOT = Path("etc/portage")
        # do your customisation under `StandboxTree=`
        if context.sandbox_tree is not None:
            installroot = (context.sandbox_tree / "installroot")
            mounts += [*(["--bind", installroot, cls.installroot] if installroot.exists() else [])]

            stage3_portage_config = context.sandbox_tree / "stage3" / CONFIG_ROOT
            if stage3_portage_config.exists():
                mounts += ["--bind", stage3_portage_config, "/" / CONFIG_ROOT]

            # local overlay repos, could be a symlink
            local_repo = context.sandbox_tree / "repos"
            if local_repo.exists():
                for d in local_repo.iterdir():
                    mounts += ["--bind", d , f"/var/db/repos/{d.stem}"]

        elif context.config.tools() is not None:
            mounts += ["--bind", context.config.tools() / CONFIG_ROOT, "/" / CONFIG_ROOT]
        elif ("/" / CONFIG_ROOT).exists():
            mounts += ["--bind", "/" / CONFIG_ROOT, "/" / CONFIG_ROOT]

        # TODO: on, at least some, non-gentoo distros kernel src tree might be at /usr/src/kernel
        (cls.installroot / "usr/src/linux").mkdir(parents=True, exist_ok=True)
        mounts += ["--ro-bind", "/usr/src/linux", cls.installroot / "usr/src/linux"]
        mounts += ["--ro-bind", "/usr/src/linux", "/usr/src/linux"]

        mounts += [ "--tmpfs", "/var/tmp/portage" ]

        return mounts

    @classmethod
    def setup(cls, context: Context, filelists: bool = True) -> None:
        cls.installroot = Path("/tmp/root")
        return

    @classmethod
    def features(cls, config: Config) -> str:
        return " ".join([*(["noman", "nodoc", "noinfo"] if config.with_docs else [])])

    @classmethod
    def cmd(cls, context: Context) -> list[PathString]:
        return [
            cls.executable(context.config),
            *(["--verbose", "--quiet-fail=n"] if ARG_DEBUG.get() else ["--quiet-build", "--quiet"]),
            f"--root={cls.installroot}",
        ]

    @classmethod
    def sandbox(
        cls,
        context: Context,
        *,
        apivfs: bool,
        options: Sequence[PathString] = (),
    ) -> AbstractContextManager[list[PathString]]:
        return sandbox_cmd(
            network=True,
            devices=True,
            tools=context.config.tools(),
            relaxed=False,
            options=[
                *context.rootoptions(cls.installroot),
                *cls.mounts(context),
                *cls.options(root=context.config.tools(), apivfs=apivfs),
                *cls.setenv(context, cls.installroot),
                *options,
            ],
        )

    @classmethod
    def invoke(
        cls,
        context: Context,
        root: PathString,
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
            cls.cmd(context) + [*(options if options is not None else []), *arguments],
            sandbox=cls.sandbox(context, apivfs=apivfs),
            env=context.config.environment,
            stdout=stdout,
        )

    @classmethod
    def sync(cls, context: Context, force: bool) -> None:
        if force or (
            not (
                (context.config.tools() / "var/db/repos/gentoo").exists()
                and any((context.config.tools() / "var/db/repos/gentoo").iterdir())
            )
        ):
            logging.info(
                textwrap.dedent("""
                you probably don't have any repos enabled including the default gentoo repos
                and you have probably passed `-ff`!
                we don't use emerge-websync either in order to allow users to use repos with sync-type=git
                petition the upstream to ship git with stage3
            """)
            )
            # run(
            #     ["emerge-webrsync", "--verbose"],
            #     sandbox=cls.sandbox(context, apivfs=False),
            #     env={'HOME': '/var/lib/portage/home'}
            # )

        if not force:
            return

        run(
            [cls.executable(context.config), "--sync"],
            check=False,
            sandbox=cls.sandbox(context, apivfs=False),
            env={"HOME": "/var/lib/portage/home"},
        )

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
        cls.invoke(context, cls.installroot, (), packages, apivfs=apivfs)

    @classmethod
    def remove(
        cls,
        context: Context,
        packages: Sequence[str],
        *,
        apivfs: bool = True,
    ) -> None:
        cls.invoke(context, cls.installroot, arguments=list(packages), options=["--unmerge"], apivfs=True)
