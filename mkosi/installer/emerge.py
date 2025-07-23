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
        # Allow the user to override autodetection with an environment variable
        emerge = config.environment.get("MKOSI_EMERGE")
        root = config.tools()

        return Path(
            emerge or find_binary("emerge", root=root) or find_binary("emerge", root=root) or "emerge"
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
            # Make sure /etc/machine-id is not overwritten by any package manager post install scripts.
            # "--ro-bind-try", Path(root) / "etc/machine-id", f"/{root}/etc/machine-id",
            # Nudge gpg to create its sockets in /run by making sure /run/user/0 exists.
            "--dir", "/run/user/0",
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
        mounts = [
            *super().mounts(context),
            # need it for things like rust-bin
            "--bind", context.config.tools() / "opt", "/opt",

            # TODO: move it to finalize_passwd_symlinks()
            # bind (as opposed to ro-bind) because build dependencies are actually
            # merged into stage3 and if they need a user/group then they need to write
            # into these
            "--bind", context.config.tools() / "etc/shadow", "/etc/shadow",
            "--bind", context.config.tools() / "etc/gshadow", "/etc/gshadow",
            "--bind", context.config.tools() / "etc/passwd", "/etc/passwd",
            "--bind", context.config.tools() / "etc/passwd-", "/etc/passwd-",
            "--bind", context.config.tools() / "etc/group", "/etc/group",
            "--bind", context.config.tools() / "etc/group-", "/etc/group-",

            "--bind", context.config.tools() / "var", "/var",
            "--bind", context.config.tools() / "var/lib", "/var/lib",
            "--bind", context.config.tools() / "var/lib/portage", "/var/lib/portage",

            "--bind", context.config.tools() / "var/db", "/var/db",
            "--bind", context.config.tools() / "var/db/pkg", "/var/db/pkg",
            "--bind", context.config.tools() / "var/cache", "/var/cache",
            "--bind", context.config.tools() / "var/cache/edb", "/var/cache/edb",
        ]  # fmt: skip
        if context.config.package_cache_dir is not None:
            mounts += ["--bind", (context.config.package_cache_dir / "var/cache/binpkgs"), "/var/cache/binpkgs"]  # fmt: skip
            mounts += ["--bind", (context.config.package_cache_dir / "var/cache/distfiles"), "/var/cache/distfiles"]  # fmt: skip
            mounts += ["--ro-bind", (context.config.package_cache_dir / "var/db/repos"), "/var/db/repos"]  # fmt: skip
            mounts += ["--ro-bind", (context.config.package_cache_dir / "var/db/repos"), cls.installroot / "var/db/repos"]  # fmt: skip

        if (context.sandbox_tree / "stage3/etc/portage").exists():
            mounts += ["--overlay-lowerdir", context.sandbox_tree / "stage3/etc/portage"]
        else:
            mounts += ["--overlay-lowerdir", context.config.tools() / "etc/portage"]

        mounts += ["--overlay-upperdir", "tmpfs", "--overlay", "/etc/portage"]

        mounts += ["--bind", context.sandbox_tree / "installroot/etc/portage", cls.installroot / "etc/portage"]  # fmt: skip
        # TODO:
        # "--ro-bind", context.keyring_dir, "/etc/portage/gnupg",

        # sys-libs/pam expects this; stuff from app-text/docbook-xsl-ns-stylesheets?
        # TODO: play with docbook-rng to see if we can avoid this
        # "--ro-bind", context.config.tools() / "etc/xml", cls.installroot / "etc/xml",
        # "--symlink", cls.installroot / "etc/xml", "/etc/xml",

        # /etc/portage/make.profile is not a symlink and will probably prevent most merges.
        mounts += ["--symlink", (context.config.tools() / "etc/portage/make.profile").readlink(), cls.installroot / "etc/portage/make.profile"]  # fmt: skip

        (cls.installroot / "usr/src/linux").mkdir(parents=True, exist_ok=True)
        mounts += ["--ro-bind", "/usr/src/linux", cls.installroot / "usr/src/linux"]  # fmt: skip

        return mounts

    @classmethod
    def setup(cls, context: Context, filelists: bool = True) -> None:
        cls.installroot = Path("/tmp/root")
        return

    @classmethod
    def features(cls, config: Config) -> str:
        return " ".join(
            [
                # Disable sandboxing in emerge because we already do it in mkosi.
                "-sandbox",
                "-pid-sandbox",
                "-ipc-sandbox",
                "-network-sandbox",
                "-news",
                "-userfetch",
                "-userpriv",
                "-usersandbox",
                "-usersync",
                "-collision-protect", # https://wiki.gentoo.org/wiki/Project:Base/Alternatives
                "protect-owned",
                "parallel-install",
                *(["noman", "nodoc", "noinfo"] if config.with_docs else []),
            ]
        )

    @classmethod
    def cmd(cls, context: Context) -> list[PathString]:
        return [
            cls.executable(context.config),
            "--buildpkg=y",
            "--usepkg=y",
            # "--getbinpkg=y",
            "--binpkg-respect-use=y",
            "--jobs",
            "--usepkg-exclude", "x11-drivers/nvidia-drivers",
            "--load-average",
            "--with-bdeps=n",
            "--with-bdeps-auto=y",
            "--changed-deps=y",
            "--changed-deps-report=y",
            "--changed-slot",
            "--changed-use",
            "--newuse",
            "--noreplace",
            "--update",
            "--verbose-conflicts",
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
            relaxed=True,
            options=[
                *context.rootoptions(cls.installroot),
                *cls.mounts(context),
                *cls.options(root=context.config.tools(), apivfs=False),
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
