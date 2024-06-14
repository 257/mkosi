# SPDX-License-Identifier: LGPL-2.1+
import os
import re
import sys
import textwrap
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from mkosi.archive import extract_tar
from mkosi.config import Config
from mkosi.context import Context
from mkosi.distributions import join_mirror
from mkosi.installer import PackageManager
from mkosi.log import ARG_DEBUG, complete_step, die, log_notice
from mkosi.run import find_binary, run
from mkosi.sandbox import Mount, sandbox_cmd
from mkosi.tree import rmtree
from mkosi.types import _FILE, CompletedProcess, PathString


class Emerge(PackageManager):
    stage3: Path

    @classmethod
    def features(cls, context: Context) -> str:
        return  " ".join([
            # No need, we're sanboxed enough !
            "-sandbox",
            "-pid-sandbox",
            "-ipc-sandbox",
            "-network-sandbox",
            "-userfetch",
            "-userpriv",
            "-usersandbox",
            "-usersync",
            # "binpkg-request-signature", # TODO: remove
            "-binpkg-signing",
            "binpkg-ignore-signature", # TODO: remove
            "parallel-install",
            *(["noman", "nodoc", "noinfo"] if context.config.with_docs else []),
        ])

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
            "emerge": cls.cmd(),
            "mkosi-install": ["emerge"],
            "mkosi-upgrade": ["emerge", "--update"],
            "mkosi-remove": ["emerge", "--unmerge"],
            "mkosi-reinstall": ["emerge"],
        }

    @classmethod
    def setup(cls, context: Context, filelists: bool = True) -> None:
        arch = context.config.distribution.architecture(context.config.architecture)

        mirror = context.config.mirror or "https://distfiles.gentoo.org"
        # http://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt
        stage3tsf_path_url = join_mirror(mirror.partition(" ")[0],
                                         f"releases/{arch}/autobuilds/latest-stage3.txt")

        with urllib.request.urlopen(stage3tsf_path_url) as r:
            # e.g.: 20230108T161708Z/stage3-amd64-nomultilib-systemd-20230108T161708Z.tar.xz
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
                        stage3_url
                    ],
                    sandbox=context.config.sandbox(
                        binary=None,
                        network=True,
                        relaxed=True,
                        options=["--bind", stage3_cache_dir, stage3_cache_dir]
                    )
                )

        cls.stage3 = stage3_cache_dir / "root"

        if not cls.stage3.exists():
            with complete_step(f"Extracting {current.name} to {cls.stage3}"):
                cls.stage3.mkdir(exist_ok=True)
                extract_tar(stage3_cache_dir / current, cls.stage3,
                            compressed=True)

        cls.do_getuto(context)

    @classmethod
    def cmd(cls, root: PathString = Path("/tmp/root")) -> list[PathString]:
        return [
            "emerge",
            "--noreplace",
            *(["--verbose", "--quiet-fail=n"] if ARG_DEBUG.get() else
                ["--quiet-build", "--quiet"]),
            f"--root={root}"
        ]

    @classmethod
    def invoke(
        cls,
        context: Context,
        arguments: Sequence[str] = (),
        options: Sequence[PathString] = (),
        root: PathString = Path("/tmp/root"),
        *,
        stdout: _FILE = sys.stdout,
    ) -> CompletedProcess:
        pkgconfigs = []
        print("config", context.config)
        for t in context.config.package_manager_trees:
            for idx, d in enumerate(["etc/portage", "var/cache/binpkgs"]):
                path = t.source / d
                if path.exists():
                    if idx == 0:
                        for (dirpath, _, filenames) in os.walk(t.source / d):
                            for fn in filenames:
                                pkgconfigs += [
                                    Mount(
                                        path / dirpath / fn,
                                        "/" / Path(dirpath).relative_to(t.source) / fn
                                    )
                                ]
                    else:
                        pkgconfigs += [Mount(t.source / d, f"/{d}")]

        if ARG_DEBUG.get():
            run(
                ["emerge", "--info"],
                check=False,
                sandbox=sandbox_cmd(
                    network=True,
                    devices=True,
                    tools=cls.stage3,
                    tools_isro=False,
                    mounts=[
                        Mount(cls.stage3 / "etc", "/etc"),
                        Mount(cls.stage3 / "var", "/var"),
                        Mount(context.root, root)
                    ] + pkgconfigs,
                ),
                env=dict(
                    FEATURES=cls.features(context),
                ) | context.config.environment,
                stdout=stdout,
            )

        return run(
            cls.cmd(root) + [
                *(options if options is not None else []),
                *arguments
            ],
            sandbox=sandbox_cmd(
                network=True,
                devices=True,
                tools=cls.stage3,
                tools_isro=False,
                mounts=[
                    Mount(cls.stage3 / "etc", "/etc"),
                    Mount(cls.stage3 / "var", "/var"),
                    Mount(context.root, root),
                ] + pkgconfigs,
                options=[
                    "--cap-add", "ALL",
                ]
            ),
            env=dict(
                BINPKG_GPG_VERIFY_GPG_HOME='/etc/portage/gnupg',
                PORTAGE_GRPNAME="root",
                PORTAGE_USERNAME="root",
                FEATURES=cls.features(context),
            ) | context.config.environment,
            stdout=stdout,
        )

    @classmethod
    def do_getuto(cls, context: Context) -> None:
        if (cls.stage3 / "gnupg").exists():
            return
        run(
            ["getuto"],
            check=False,
            sandbox=sandbox_cmd(
                network=True,
                devices=True,
                tools=cls.stage3,
                mounts=[
                    Mount(cls.stage3 / "etc", "/etc"),
                    Mount(cls.stage3 / "var", "/var"),
                ],
                options=[
                    "--cap-add", "ALL",
                ]
            ),
        )


    @classmethod
    def sync(cls, context: Context) -> None:
        if not ((cls.stage3 / "var/db/repos/gentoo").exists() and
            any((cls.stage3 / "var/db/repos/gentoo").iterdir())):
            run(
                ["emerge-webrsync"],
                sandbox=sandbox_cmd(
                    network=True,
                    devices=True,
                    tools=cls.stage3,
                    mounts=[
                        Mount(cls.stage3 / "etc", "/etc"),
                        Mount(cls.stage3 / "var", "/var"),
                    ],
                    options=[
                        "--cap-add", "ALL",
                    ]
                ),
                env={'HOME': '/var/lib/portage/home'}
            )
        run(
            ["emerge", "--sync"],
            check=False,
            sandbox=sandbox_cmd(
                network=True,
                devices=True,
                tools=cls.stage3,
                mounts=[
                    Mount(cls.stage3 / "etc", "/etc"),
                    Mount(cls.stage3 / "var", "/var"),
                ],
                options=[
                    "--cap-add", "ALL",
                ]
            ),
            env={
                'HOME': '/var/lib/portage/home',
                'PORTAGE_GRPNAME': "root",
                'PORTAGE_USERNAME': "root",
                'FEATURES': cls.features(context),
            }
        )


    @classmethod
    def createrepo(cls, context: Context) -> None:
        run(["createrepo_c", context.packages],
            sandbox=context.sandbox(binary=None, options=["--bind",
                                                          context.packages,
                                                          context.packages]))

        (context.pkgmngr / "etc/portage/repos.conf/mkosi-local.conf").write_text(
            textwrap.dedent(
                """\
                [mkosi]
                name=mkosi
                baseurl=file:///work/packages
                priority=50
                """
            )
        )

        cls.sync(context)
