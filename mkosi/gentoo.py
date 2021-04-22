import multiprocessing
import os
import re
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path
from textwrap import dedent
from typing import Dict, List, Sequence

from .backend import (
    ARG_DEBUG,
    CommandLineArguments,
    MkosiPrinter,
    OutputFormat,
    die,
    run_workspace_command,
)

# https://github.com/python/mypy/issues/5732
kimg_path: str


class Gentoo:
    arch_profile: Path
    baselayout_use: Path
    DEFAULT_NSPAWN_PARAMS: List[str]
    emerge_default_opts: List[str]
    arch: str
    emerge_vars: Dict[str, str]
    pkgs_boot: List[str]
    pkgs_sys: List[str]
    pkgs_fs: List[str]
    grub_platforms: List[str]
    UNINSTALL_IGNORE: List[str]
    root: Path
    portage_cfg_dir: Path
    profile_path: Path
    custom_profile_path: Path
    ebuild_sh_env_dir: Path
    dracut_atom = "sys-kernel/dracut"


    EMERGE_UPDATE_OPTS = [
        "--update",
        "--tree",
        "--changed-use",
        "--newuse",
        "--deep",
        "--with-bdeps=y",
        "--complete-graph-if-new-use=y",
    ]

    portage_use_flags = [
        "systemd",  # 'systemd' is hard dependancy?
        "initramfs",
        "git",  # 'git' for sync-type=git
        "symlink",  # 'symlink' for kernel
        "sdl",
        "-filecaps",
        "-savedconfig",
        "-split-bin",
        "-split-sbin",
        "-split-usr",
    ]

    # TODO: portage_features.add("ccache")
    portage_features = [
        # -user* are required for access to USER_CONFIG_PATH
        "-userfetch",
        "-userpriv",
        "-usersync",
        "-usersandbox",
        "-sandbox",
        "-pid-sandbox",  # -pid-sandbox is required for cross-compile scenarios
        "-network-sandbox",
        "parallel-install",
        "buildpkg",
        "binpkg-multi-instance",
        "-binpkg-docompress",
        "getbinpkg",
        "-candy",
    ]

    ARCHITECTURES = {
        "x86_64": ("amd64", "arch/x86/boot/bzImage"),
        "aarch64": ("arm64", "arch/arm64/boot/Image.gz"),
        "armv7l": ("arm", "arch/arm/boot/zImage"),
    }


    @staticmethod
    def try_import_portage() -> Dict[str, str]:
        NEED_PORTAGE_MSG = "You need portage(5) for Gentoo"
        PORTAGE_INSTALL_INSTRUCTIONS = """\
        # Following is known to work on most systemd-based systems:
        sudo tee /usr/lib/sysusers.d/acct-group-portage.conf > /dev/null <<- EOF
        g portage 250
        EOF
        sudo tee /usr/lib/sysusers.d/acct-user-portage.conf > /dev/null <<- EOF
        u portage 250:portage System user; portage /var/lib/portage/home -
        EOF
        sudo systemd-sysusers --no-pager

        sudo install --owner=portage --group=portage --mode=0755 --directory /var/db/repos
        sudo install --owner=portage --group=portage --mode=0755 --directory /etc/portage/repos.conf
        sudo install --owner=portage --group=portage --mode=0755 --directory /var/cache/binpkgs
        sudo tee /etc/portage/repos.conf/eselect-repo.conf > /dev/null <<- EOF
        [gentoo]
        location = /var/db/repos/gentoo
        sync-type = git
        sync-uri = https://anongit.gentoo.org/git/repo/gentoo.git
        EOF

        git clone https://anongit.gentoo.org/git/proj/portage.git --depth=1
        cd portage
        sudo tee setup.cfg > /dev/null <<- EOF
        [build_ext]
        portage-ext-modules=true
        EOF

        python setup.py build_ext --inplace --portage-ext-modules
        sudo python setup.py install

        sudo ln -s ../../var/db/repos/gentoo/profiles/default/linux/amd64/17.1/no-multilib/systemd /etc/portage/make.profile
        """
        try:
            from portage.const import (  # type: ignore
                CUSTOM_PROFILE_PATH,
                EBUILD_SH_ENV_DIR,
                PROFILE_PATH,
                USER_CONFIG_PATH,
            )
        except ImportError as e:
            from .backend import MkosiException
            MkosiPrinter.warn(f"{NEED_PORTAGE_MSG}")
            MkosiPrinter.info(PORTAGE_INSTALL_INSTRUCTIONS)
            raise MkosiException(e)

        return dict(profile_path = PROFILE_PATH,
                    custom_profile_path = CUSTOM_PROFILE_PATH,
                    ebuild_sh_env_dir = EBUILD_SH_ENV_DIR,
                    portage_cfg_dir = USER_CONFIG_PATH)


    def __init__(
        self,
        args: CommandLineArguments,
        root: Path,
        do_run_build_script: bool,
    ) -> None:

        from portage.package.ebuild.config import config  # type: ignore

        self.portage_cfg = config(
            config_root=str(root), target_root=str(root), sysroot=str(root), eprefix=None
        )

        if "build-script" in ARG_DEBUG:
            for k, v in self.portage_cfg.items():
                print(f"{k} = {v}")

        ret = self.try_import_portage()

        self.profile_path = root / ret["profile_path"]
        self.custom_profile_path = root / ret["custom_profile_path"]
        self.ebuild_sh_env_dir = root / ret["ebuild_sh_env_dir"]
        self.portage_cfg_dir = root / ret["portage_cfg_dir"]

        self.portage_cfg_dir.mkdir(parents=True, exist_ok=True)


        self.DEFAULT_NSPAWN_PARAMS = [
            "--capability=CAP_SYS_ADMIN,CAP_MKNOD",
            "--tmpfs=/sys",
            f"--bind={self.portage_cfg['PORTDIR']}",
            f"--bind={self.portage_cfg['DISTDIR']}",
            f"--bind={self.portage_cfg['PKGDIR']}",
        ]

        jobs = multiprocessing.cpu_count()
        self.emerge_default_opts = [
            "--buildpkg=y",
            "--usepkg=y",
            "--keep-going=y",
            f"--jobs={jobs}",
            f"--load-average={jobs-1}",
            "--nospinner",
        ]
        if "build-script" in ARG_DEBUG:
            self.emerge_default_opts += ["--verbose", "--quiet=n", "--quiet-fail=n"]
        else:
            self.emerge_default_opts += ["--quiet-build", "--quiet"]

        global kimg_path
        self.arch, kimg_path = self.ARCHITECTURES[args.architecture or "x86_64"]

        # GENTOO_UPSTREAM : we only support systemd profiles! and only the no-multilib flaivour , for now;
        # GENTOO_UPSTREAM : wait for fix upstream: https://bugs.gentoo.org/792081
        # GENTOO_TODO     : add args.profile switch for this? (multilib vs. nomultilib)
        # GENTOO_DONTMOVE : could be done inside set_profile, however stage3_fetch() will be needing this if we want to allow users to pick profile
        self.arch_profile = Path(f"profiles/default/linux/{self.arch}/{args.release}/no-multilib/systemd")

        self.pkgs_sys = ["@world"]

        self.pkgs_fs = ["sys-fs/dosfstools"]
        if args.output_format in (OutputFormat.subvolume, OutputFormat.gpt_btrfs):
            self.pkgs_fs += ["sys-fs/btrfs-progs"]
        elif args.output_format == OutputFormat.gpt_xfs:
            self.pkgs_fs += ["sys-fs/xfsprogs"]
        elif args.output_format == OutputFormat.gpt_squashfs:
            self.pkgs_fs += ["sys-fs/squashfs-tools"]

        if args.encrypt:
            self.pkgs_fs += ["cryptsetup", "device-mapper"]

        self.grub_platforms = list()
        if not do_run_build_script and args.bootable:
            if args.esp_partno:
                self.pkgs_boot = ["sys-kernel/installkernel-systemd-boot"]
            elif args.bios_partno:
                self.pkgs_boot = ["sys-boot/grub"]
                self.grub_platforms = ["coreboot", "qemu", "pc"]

            self.pkgs_boot += ["sys-kernel/gentoo-kernel-bin", "sys-firmware/edk2-ovmf"]

        self.UNINSTALL_IGNORE = ["/bin", "/sbin", "/lib", "/lib64"]

        # GENTOO_DONTMOVE: self.grub_platforms, for instance, must be set
        self.emerge_vars = {
            "BOOTSTRAP_USE": " ".join(self.portage_use_flags),
            "FEATURES": " ".join(self.portage_features),
            "EGIT_CLONE_TYPE": "shallow",
            "GRUB_PLATFORMS": " ".join(self.grub_platforms),
            "UNINSTALL_IGNORE": " ".join(self.UNINSTALL_IGNORE),
            "USE": " ".join(self.portage_use_flags),
        }

        self.fetch_fix_stage3(root)
        self.set_profile(args)
        self.set_default_repo()
        self.unmask_arch()
        self.whitelist_licenses()
        self.provide_patches()
        self.set_useflags()
        self.mkosi_conf()
        self.invoke_emerge(args, root, inside_stage3=False, actions=["--sync"])
        self.baselayout(args, root)
        self.update_stage3(args, root)

        if "build-script" in ARG_DEBUG:
            self.invoke_emerge(args, root, actions=["--info"])

        return


    def fetch_fix_stage3(self, root: Path) -> None:
        """usrmerge tracker bug: https://bugs.gentoo.org/690294"""

        # e.g.: http://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt
        stage3tsf_path_url = urllib.parse.urljoin(
            self.portage_cfg["GENTOO_MIRRORS"].partition(" ")[0],
            f"releases/{self.arch}/autobuilds/latest-stage3.txt",
        )
        # GENTOO_UPSTREAM: wait for fix upstream: https://bugs.gentoo.org/792081
        # and more... so we can gladly escape all this hideousness!
        with urllib.request.urlopen(stage3tsf_path_url) as r:
            args_profile = "nomultilib"
            # 20210711T170538Z/stage3-amd64-nomultilib-systemd-20210711T170538Z.tar.xz 214470580
            regexp = f"^[0-9TZ]+[/]stage3-{self.arch}-{args_profile}-systemd-[0-9TZ]+[.]tar[.]xz"
            all_lines = r.readlines()
            for line in all_lines:
                m = re.match(regexp, line.decode("utf-8"))
                if m:
                    stage3_tar = Path(m.group(0))
                    break
            else:
                die("profile names changed upstream?")

        stage3_url_path = urllib.parse.urljoin(
            self.portage_cfg["GENTOO_MIRRORS"],
            f"releases/{self.arch}/autobuilds/{stage3_tar}",
        )
        stage3_tar_path = self.portage_cfg["DISTDIR"] / stage3_tar
        if not stage3_tar_path.is_file():
            MkosiPrinter.print_step(f"Fetching {stage3_url_path}")
            stage3_tar_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(stage3_url_path, stage3_tar_path)

        with tarfile.open(stage3_tar_path) as tfd:
            MkosiPrinter.print_step(f"Extracting {stage3_tar.name}")
            # GENTOO: usrmerge mess
            bins = []
            usr = []
            usrreg = "^[.][/](etc|usr|var)"
            binreg = "^[.][/](sbin|bin|lib|lib64)"
            devreg = "^[.][/]dev"
            runreg = "^[.][/]run"

            members = tfd.getmembers()
            usrpath = root / "usr"

            ignore = ["./var/run", "./var/lock", "./bin/awk"]
            for dir in self.UNINSTALL_IGNORE:
                ignore += [dir] + [f"./usr/{dir}"]

            for tarinfo in members:
                if tarinfo.name in ignore or re.match(devreg, tarinfo.name) or re.match(runreg, tarinfo.name):
                    if "build-script" in ARG_DEBUG:
                        print(f"{tarinfo.name} -> /dev/null")
                    continue
                elif (
                    tarinfo.issym()
                    and (os.path.dirname(tarinfo.name) == "./usr/bin")
                    and (os.path.dirname(tarinfo.linkname) == "../../bin")
                ):
                    if "build-script" in ARG_DEBUG:
                        print(f"{tarinfo.name} -> {tarinfo.linkname} -> /dev/null")
                    continue
                elif re.match(usrreg, tarinfo.name):
                    usr.append(tarinfo)
                elif re.match(binreg, tarinfo.name):
                    bins.append(tarinfo)
                else:
                    print(f"{tarinfo.name} -> /dev/null")
                    continue

            try:
                tfd.extractall(usrpath, bins, numeric_owner=True)
                tfd.extractall(root, usr, numeric_owner=True)
            except FileExistsError:
                pass

        return

    def set_profile(self, args: CommandLineArguments) -> None:
        # be careful not to overwrite user's profile, (via skeleton)
        if not self.profile_path.is_symlink():
            MkosiPrinter.print_step(f"{args.distribution} setting Profile")
            self.profile_path.symlink_to(self.portage_cfg["PORTDIR"] / self.arch_profile)

        return

    def set_default_repo(self) -> None:
        eselect_repo_conf = self.portage_cfg_dir / "repos.conf"
        eselect_repo_conf.mkdir(exist_ok=True)
        eselect_repo_conf.joinpath("eselect-repo.conf").write_text(
            dedent(
                f"""\
                    [gentoo]
                    location = {self.portage_cfg["PORTDIR"]}
                    sync-uri = https://anongit.gentoo.org/git/repo/gentoo.git
                    sync-type = git
                    sync-dept = 1
                    """
            )
        )

        return

    def unmask_arch(self) -> None:
        package_accept_keywords = self.portage_cfg_dir / "package.accept_keywords"
        package_accept_keywords.mkdir(exist_ok=True)

        package_accept_keywords.joinpath("mkosi").write_text(
            dedent(
                # homed is still ~ARCH
                f"""\
                sys-auth/pambase ~{self.arch}
                # sys-kernel/gentoo-kernel-bin ~{self.arch}
                # virtual/dist-kernel ~{self.arch}
                sys-apps/baselayout ~{self.arch}
                """
            )
        )
        package_accept_keywords.joinpath("bug765208").write_text(f"<{self.dracut_atom}-56 ~{self.arch}\n")

        return

    def whitelist_licenses(self) -> None:
        package_license = self.portage_cfg_dir / "package.license"
        package_license.write_text("sys-kernel/linux-firmware @BINARY-REDISTRIBUTABLE\n")

        return

    def provide_patches(self) -> None:
        patches_dir = self.portage_cfg_dir / "patches"
        patches_dir.mkdir(exist_ok=True)

        return

    def set_useflags(self) -> None:
        self.custom_profile_path.mkdir(exist_ok=True)
        self.custom_profile_path.joinpath("use.force").write_text(
            dedent(
                """\
                    -split-bin
                    -split-sbin
                    -split-usr
                    """
            )
        )

        package_use = self.portage_cfg_dir / "package.use"
        package_use.mkdir(exist_ok=True)

        self.baselayout_use = package_use.joinpath("baselayout")
        self.baselayout_use.write_text("sys-apps/baselayout build\n")
        package_use.joinpath("grub").write_text("sys-boot/grub device-mapper truetype\n")
        package_use.joinpath("systemd").write_text(
            # repart for usronly
            dedent(
                """\
                # sys-apps/systemd http
                # sys-apps/systemd cgroup-hybrid

                # MKOSI: Failed to open "/usr/lib/systemd/boot/efi": No such file or directory
                sys-apps/systemd gnuefi

                # sys-apps/systemd -pkcs11
                # sys-apps/systemd importd lzma
                sys-apps/systemd homed cryptsetup -pkcs11
                # MKOSI: usronly
                sys-apps/systemd repart
                # sys-apps/systemd -cgroup-hybrid
                # sys-apps/systemd vanilla
                # sys-apps/systemd policykit
                # MKOSI: make sure we're init (no openrc)
                sys-apps/systemd sysv-utils
                # sys-fs/lvm2 device-mapper-only -thin
                """
            )
        )

        return

    def mkosi_conf(self) -> None:
        package_env = self.portage_cfg_dir / "package.env"
        package_env.mkdir(exist_ok=True)
        self.ebuild_sh_env_dir.mkdir(exist_ok=True)

        # apply whatever we put in mkosi_conf to runs invokation of emerge
        package_env.joinpath("mkosi.conf").write_text("*/*    mkosi.conf\n")

        # we use this so we don't need to touch upstream files
        # we also use this for documenting build environment as much as possible

        emerge_vars_str = ""
        emerge_vars_str += "\n".join(f'{k}="${{{k}}} {v}"' for k, v in self.emerge_vars.items())

        self.ebuild_sh_env_dir.joinpath("mkosi.conf").write_text(
            dedent(
                f"""\
                    # MKOSI: these were used during image creation...
                    # and some more! see under package.*/
                    #
                    # usrmerge (see all under profile/)
                    {emerge_vars_str}
                    """
            )
        )

        return

    def invoke_emerge(
        self,
        args: CommandLineArguments,
        root: Path,
        inside_stage3: bool = True,
        pkgs: Sequence[str] = (),
        actions: Sequence[str] = (),
        opts: Sequence[str] = (),
    ) -> None:
        if not inside_stage3:
            from _emerge.main import emerge_main  # type: ignore

            PREFIX_OPTS: List[str] = []
            if "--sync" not in actions:
                PREFIX_OPTS = [
                    f"--config-root={root.resolve()}",
                    f"--root={root.resolve()}",
                    f"--sysroot={root.resolve()}",
                ]

            emerge_main([*pkgs, *opts, *actions] + PREFIX_OPTS + self.emerge_default_opts)
        else:

            cmd = ["/usr/bin/emerge", *pkgs, *self.emerge_default_opts, *opts, *actions]

            MkosiPrinter.print_step("Invoking emerg(1) inside stage3")
            run_workspace_command(
                args,
                root,
                cmd,
                network=True,
                env=self.emerge_vars,
                nspawn_params=self.DEFAULT_NSPAWN_PARAMS,
            )

            return

    def baselayout(self, args: CommandLineArguments, root: Path) -> None:
        # TOTHINK: sticky bizness when when image profile != host profile
        self.invoke_emerge(args, root, pkgs=["sys-apps/baselayout"])
        cmdline = ["env-update"]
        run_workspace_command(args, root, cmdline, nspawn_params=self.DEFAULT_NSPAWN_PARAMS)

        return

    def update_stage3(self, args: CommandLineArguments, root: Path) -> None:
        # baselayout gets remerged here because of USE=build which overrides
        # /etc/os-release; (see below)
        self.invoke_emerge(args, root, pkgs=self.pkgs_sys, opts=self.EMERGE_UPDATE_OPTS)

        # DONTMOVE: baselayout is settled down by now, dracut needs this for
        # version_id in /etc/os-release data (UKI)
        # GENTOO_BUG: https://bugs.gentoo.org/788190
        with root.joinpath("etc/os-release").open("a") as f:
            f.write(f"VERSION_ID={args.release}\n")

        # it's a possibility that updating @world will update
        # openssh/baselaout... so do these cleanups at the end
        self.baselayout_use.unlink()
        # FIXME?: without this we get the following
        # Synchronizing state of sshd.service with SysV service script with /lib/systemd/systemd-sysv-install.
        # Executing: /lib/systemd/systemd-sysv-install --root=/var/tmp/mkosi-2b6snh_u/root enable sshd
        # chroot: failed to run command ‘/usr/sbin/update-rc.d’: No such file or directory
        root.joinpath("etc/init.d/sshd").unlink()

        return

    def _dbg(self, args: CommandLineArguments, root: Path) -> None:
        """this is for dropping into shell to see what's wrong"""

        cmdline = ["/bin/sh"]
        run_workspace_command(
            args,
            root,
            cmdline,
            network=True,
            nspawn_params=self.DEFAULT_NSPAWN_PARAMS,
        )

        return
