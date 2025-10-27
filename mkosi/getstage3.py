#!/usr/bin/env python

import configparser
import os
import re
import urllib.parse
import urllib.request
from argparse import SUPPRESS, ArgumentParser, Namespace
from collections.abc import Sequence
from pathlib import Path
from textwrap import dedent

from mkosi.archive import extract_tar
from mkosi.log import complete_step, die, log_notice, log_setup
from mkosi.run import run
from mkosi.tree import rmtree

cwd = Path(os.getcwd())

def join_mirror(mirror: str, link: str) -> str:
    # urljoin() behaves weirdly if the base does not end with a / or the path
    # starts with a / so fix them up as needed.
    if not mirror.endswith("/"):
        mirror = f"{mirror}/"
    link = link.removeprefix("/")

    return urllib.parse.urljoin(mirror, link)

def curl(url: str, output_dir: Path, log: bool = True) -> None:
    run(
        [
            "curl",
            "--location",
            "--output-dir", cwd / output_dir,
            "--remote-name",
            "--no-progress-meter",
            "--fail",
            *(["--silent"] if not log else []),
            url,
        ],
        log=log,
    )  # fmt: skip

def setup(args: Namespace) -> None:
    log_setup()
    mirror = "https://distfiles.gentoo.org"

    # http://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt
    stage3tsf_path_url = join_mirror(
        mirror.partition(" ")[0],
        f"releases/{args.machine}/autobuilds/latest-stage3.txt",
    )

    stage3_latest = ""
    with urllib.request.urlopen(stage3tsf_path_url) as r:
        # TODO: construct from args.{machine,profile}
        # e.g.: 20250322T105044Z/stage3-amd64-nomultilib-systemd-20250322T105044Z.tar.xz
        regexp = (
            rf"^[0-9]+T[0-9]+Z/stage3-{args.machine}-{args.profile.replace('/','-')}-[0-9]+T[0-9]+Z\.tar\.xz"
        )
        lines_decoded = []
        for line in r.readlines():
            decoded_line  = line.decode("utf-8")
            if m := re.match(regexp, decoded_line):
                stage3_latest = Path(m.group(0))
                log_notice(f"Found the latest stage3 snapshot: {stage3_latest}")
                break
            else:
                lines_decoded.append(decoded_line)
        else:
            from pprint import pformat
            log_notice(pformat(lines_decoded))
            die(dedent(
                    f"""\
                    profile names changed upstream?
                    we tried {regexp} on whatever we got above from {stage3tsf_path_url}
                    """
            ))

    stage3_url = join_mirror(mirror, f"releases/{args.machine}/autobuilds/{stage3_latest}")

    latest = Path(stage3_latest)
    output_dir = Path("/var/cache/distfiles/stage3s")
    stage3_tar = output_dir / latest.name
    args.toolstree.mkdir(parents=True, exist_ok=True)
    if not stage3_tar.exists():
        rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        with complete_step(f"Fetching the {latest.name} into {output_dir}"):
            curl(stage3_url, output_dir)

    if not (args.toolstree / "usr").exists():
        with complete_step(f"Extracting {stage3_tar.name} to {args.toolstree}"):
            extract_tar(stage3_tar, args.toolstree, options=["--xz"])

    resolv = (args.toolstree / "etc/resolv.conf")
    if not resolv.exists():
        resolv.symlink_to("/etc/resolv.conf")

def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="SYNC", description="sync",
                            argument_default=SUPPRESS)

    parser.add_argument("--config-file", dest="cfgf", type=Path,
                        default='mkosi.conf.d/img.conf',
                        help="""path to the image definition file.
                        sections we care about, so far, are:
                            - `Build.ToolsTree` (for saving stage3)
                            - `Content.Locale` (for setting which locales we care about)
                        """)
    # './../var/db/repos/gentoo/profiles/default/linux/amd64/23.0/nomultilib/systemd'
    parser.add_argument("--machine", dest="machine", type=str,
                        default='amd64',
                        help="""
                        machine variant (only amd64 atm)
                        - use `eselect profile list or
                        - or `ls -l /var/db/repos/gentoo/profiles/default/linux`
                        """)
    parser.add_argument("--profile", dest="profile", type=str,
                        default='nomultilib/systemd',
                        help="""
                        Gentoo profile to use
                        to see available options either:
                        - use `eselect profile list or
                        - or
                        `ls -l /var/db/repos/gentoo/profiles/default/linux/<machine>`
                        """)

    return parser

def parse_cfg(args: Namespace) -> Namespace:
    cfg = configparser.RawConfigParser()
    cfg.read(Path.cwd() / args.cfgf)
    args.output = cfg.get('Output', 'output')
    args.output_dir = cfg.get('Output', 'outputdirectory', raw=True)
    args.toolstree = cfg.get('Build', 'toolstree', raw=True)

    # replace Specifiers defined in mkosi.md
    # atm, we only care about:
    # - '%O' OutputDirectory
    # - '%o' Output
    if '%o' in args.output_dir:
        args.output_dir = args.output_dir.replace('%o', args.output)
    if '%O' in args.toolstree:
        args.toolstree = args.toolstree.replace('%O', args.output_dir)
    args.toolstree = Path(args.toolstree)

    return args

def parse_args(argv: Sequence[str] | None = None) -> Namespace:
    args_parsed, _ = create_parser().parse_known_args(argv)
    return parse_cfg(args_parsed)

def main():
    setup(parse_args())

if __name__ == "__main__":
    main()
