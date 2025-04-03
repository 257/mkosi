# SPDX-License-Identifier: LGPL-2.1+
import logging
import re
import sys
import textwrap
import urllib.request
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from mkosi.archive import extract_tar
from mkosi.config import Config
from mkosi.context import Context
from mkosi.distributions import join_mirror
from mkosi.installer import PackageManager
from mkosi.log import ARG_DEBUG, complete_step, die, log_notice
from mkosi.run import (
    CompletedProcess,
    apivfs_options,
    finalize_passwd_symlinks,
    find_binary,
    run,
    sandbox_cmd,
    workdir,
)
from mkosi.tree import copy_tree, rmtree
from mkosi.util import _FILE, PathString


class Emerge(PackageManager):
    @classmethod
    def executable(cls, config: Config) -> str:
        # Allow the user to override autodetection with an environment variable
        emerge = config.environment.get("MKOSI_EMERGE")
        root = config.tools()

        return Path(emerge or find_binary("emerge", root=root) or
                    find_binary("emerge", root=root) or "emerge").name

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
            # Some package managers (e.g. dpkg) read from the host's /etc/passwd instead of the buildroot's
            # /etc/passwd so we symlink /etc/passwd from the buildroot to make sure it gets used.
            *(finalize_passwd_symlinks(root) if apivfs else []),
        ]  # fmt: skip

    @classmethod
    def setenv(cls, context: Context, root: PathString) -> list[PathString]:
        return [
            "--setenv", "PORTAGE_CONFIGROOT", str(root),
            "--setenv", "GPG_VERIFY_USER_DROP", "root",
            "--setenv", "GPG_VERIFY_GROUP_DROP", "root",
            "--setenv", "FEATURES", cls.features(context.config)
        ]

    @classmethod
    def mounts(cls, context: Context) -> list[PathString]:
        mounts = [
            *super().mounts(context),
            # TODO: move it to finalize_passwd_symlinks()
            # bind (as opposed to ro-bind) because build dependencies are actually
            # merged into stage3 and if they need a user/group then they need to write
            # into these
            "--bind", cls.stage3 / "etc/shadow", "/etc/shadow",
            "--bind", cls.stage3 / "etc/gshadow", "/etc/gshadow",
            "--bind", cls.stage3 / "etc/passwd", "/etc/passwd",
            "--bind", cls.stage3 / "etc/group", "/etc/group",

            "--bind", cls.stage3 / "var/cache/edb", "/var/cache/edb",
            "--bind", cls.stage3 / "var/lib/portage", "/var/lib/portage",
            "--bind", cls.stage3 / "var/db/pkg", "/var/db/pkg",
            "--bind", context.sandbox_tree / "etc/portage", cls.installroot / "etc/portage",
            "--bind", cls.stage3 / "etc/portage", "/etc/portage",
            "--ro-bind", context.sandbox_tree / "etc/portage/package.accept_keywords", "/etc/portage/package.accept_keywords",
            "--ro-bind", context.sandbox_tree / "etc/portage/package.use", "/etc/portage/package.use",
            "--ro-bind", context.sandbox_tree / "etc/portage/package.mask", "/etc/portage/package.mask",
            # "--tmpfs", "/var/tmp/portage",
            # "--overlay-lowerdir", cls.stage3 / "etc/portage", "/etc/portage",
            # "--overlay-lowerdir", context.sandbox_tree / "etc/portage", "/etc/portage",
            # "--overlay-upperdir", "tmpfs",
            # "--overlay", "/etc/portage",
            # /etc/portage/make.profile is not a symlink and will probably prevent most merges.
            "--symlink", (cls.stage3 / "etc/portage/make.profile").readlink(), cls.installroot / "etc/portage/make.profile",

            # TODO:
            # "--ro-bind", context.keyring_dir, "/etc/portage/gnupg",
            "--ro-bind", context.sandbox_tree / "etc/portage/gnupg", "/etc/portage/gnupg",

        ]  # fmt: skip
        if context.config.package_cache_dir is not None:
            mounts += ["--bind", (context.config.package_cache_dir / "cache/binpkgs"), "/var/cache/binpkgs"]
            mounts += ["--bind", (context.config.package_cache_dir / "cache/distfiles"), "/var/cache/distfiles"]
            mounts += ["--ro-bind", (context.config.package_cache_dir / "db/repos"), "/var/db/repos"]
            mounts += ["--ro-bind", (context.config.package_cache_dir / "db/repos"), cls.installroot / "var/db/repos"]

        return mounts

    @classmethod
    def setup(cls, context: Context, filelists: bool = True) -> None:
        arch = context.config.distribution.architecture(context.config.architecture)

        mirror = context.config.mirror or "https://distfiles.gentoo.org"
        # http://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt
        stage3tsf_path_url = join_mirror(
            mirror.partition(" ")[0],
            f"releases/{arch}/autobuilds/latest-stage3.txt",
        )

        with urllib.request.urlopen(stage3tsf_path_url) as r:
            # e.g.: 20250322T105044Z/stage3-amd64-nomultilib-systemd-20250322T105044Z.tar.xz
            regexp = rf"^[0-9]+T[0-9]+Z/stage3-{arch}-nomultilib-systemd-[0-9]+T[0-9]+Z\.tar\.xz"
            all_lines = r.readlines()
            for line in all_lines:
                if (m := re.match(regexp, line.decode("utf-8"))):
                    stage3_latest = Path(m.group(0))
                    break
            else:
                die("profile names changed upstream?")

        stage3_url = join_mirror(mirror, f"releases/{arch}/autobuilds/{stage3_latest}")

        current = Path(stage3_latest)
        stage3_cache_dir = context.config.package_cache_dir_or_default() / "stage3"
        # stage3_cache_dir = context.config.tools()
        stage3_cache_dir.mkdir(parents=True, exist_ok=True)

        if not (stage3_cache_dir / current).exists():
            output_dir = stage3_cache_dir / current.parent
            with complete_step(
                f"Fetching the latest stage3 snapshot into {stage3_cache_dir / current}"
            ):
                for i in stage3_cache_dir.iterdir():
                    if i.is_dir() and i != output_dir:
                        log_notice(f"found older stage3 {i.name}, removing...")
                        rmtree(i)

                output_dir.mkdir(parents=True, exist_ok=True)
                run(
                    [
                        "curl",
                        "--location",
                        "--progress-bar",
                        "--output-dir", output_dir,
                        "--remote-name",
                        "--fail",
                        stage3_url
                    ],
                    sandbox=context.config.sandbox(
                        network=True,
                        relaxed=True,
                        options=["--bind", stage3_cache_dir, workdir(stage3_cache_dir)]
                    )
                )

        cls.stage3 = stage3_cache_dir / "root"
        # FIXME:
        cls.installroot = Path("/tmp/root")

        if not cls.stage3.exists():
            with complete_step(f"Extracting {current.name} to {cls.stage3}"):
                cls.stage3.mkdir(exist_ok=True)
                extract_tar(stage3_cache_dir / current, cls.stage3, options=["--xz"])

        if context.config.tools_tree:
            copy_tree(context.config.tools_tree, cls.stage3, sandbox=context.sandbox)

    @classmethod
    def features(cls, config: Config) -> str:
        return ' '.join([
                # Disable sandboxing in emerge because we already do it in mkosi.
                '-sandbox',
                '-pid-sandbox',
                '-ipc-sandbox',
                '-network-sandbox',
                '-news',
                '-userfetch',
                '-userpriv',
                '-usersandbox',
                '-usersync',
                'parallel-install',
                *(['noman', 'nodoc', 'noinfo'] if config.with_docs else []),
            ])

    @classmethod
    def cmd(cls, context: Context) -> list[PathString]:
        return [
            cls.executable(context.config),
            "--buildpkg=y",
            "--usepkg=y",
            # "--getbinpkg=y",
            "--binpkg-respect-use=y",
            "--jobs",
            "--load-average",
            "--root-deps=rdeps",
            "--with-bdeps-auto=n",
            "--verbose-conflicts",
            "--noreplace",
            "--update",
            "--newuse",
            *(["--verbose", "--quiet-fail=n"] if ARG_DEBUG.get()
              else ["--quiet-build", "--quiet"]),
            f"--root={cls.installroot}"
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
            tools=cls.stage3,
            options=[
                *context.rootoptions(cls.installroot),
                *cls.mounts(context),
                *cls.options(root="/", apivfs=False),
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
            cls.cmd(context) + [
                *(options if options is not None else []),
                *arguments
            ],
            sandbox=cls.sandbox(context, apivfs=apivfs),
            env=context.config.environment,
            stdout=stdout,
        )

    @classmethod
    def sync(cls, context: Context, force: bool) -> None:

        if force or (not ((cls.stage3 / "var/db/repos/gentoo").exists() and
                          any((cls.stage3 / "var/db/repos/gentoo").iterdir()))):
            logging.info(textwrap.dedent("""
                you probably don't have any repos enabled including the default gentoo repos
                and you have probably passed `-ff`!
                we don't use emerge-websync either because to allow users use repos with
                sync-type=git
                petition the upstream to ship git with stage3
            """))
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
            env={'HOME': '/var/lib/portage/home'}
        )


    @classmethod
    def createrepo(cls, context: Context) -> None:
        cls.sync(context, True if context.args.force==2 else False)

    @classmethod
    def install(
        cls,
        context: Context,
        packages: Sequence[str],
        *,
        apivfs: bool = True,
    ) -> None:
        cls.invoke(context, cls.installroot, (), packages, apivfs=apivfs)
