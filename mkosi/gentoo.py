import multiprocessing
import os
import re
import tarfile
import urllib.parse
import urllib.request
from textwrap import dedent
from typing import List, Set

from .backend import (
    ARG_DEBUG,
    CommandLineArguments,
    MkosiPrinter,
    OutputFormat,
    die,
    run_workspace_command,
)

NO_PORTAGE_DIE_MSG = "You need portage(5) - the heart of Gentoo"
PORTAGE_GIT_URL = "https://gitweb.gentoo.org/proj/portage.git"
try:
    from portage.const import (  # type: ignore
        CUSTOM_PROFILE_PATH,
        EBUILD_SH_ENV_DIR,
        PROFILE_PATH,
        USER_CONFIG_PATH,
    )
    from portage.package.ebuild.config import config  # type: ignore
except ImportError:
    die(f"{NO_PORTAGE_DIE_MSG} : {PORTAGE_GIT_URL}")


# https://github.com/python/mypy/issues/5732
kimg_path: str


class Gentoo:
    arch_profile: str
    emerge_default_opts: Set[str]
    EMERGE_UPDATE_OPTS: Set[str]
    arch: str
    package_use: str
    pkgs_boot: List[Set[str]]
    pkgs_sys: Set[str]
    pkgs_fs: Set[str]
    grub_platforms: Set[str]
    portage_features: Set[str]
    portage_use_flags: Set[str]
    UNINSTALL_IGNORE: Set[str]
    dracut_atom = "sys-kernel/dracut"

    def __init__(self, args: CommandLineArguments, root: str, do_run_build_script: bool) -> None:
        self.portage_cfg = config(config_root=root, target_root=root, sysroot=root, eprefix=None)
        if "build-script" in ARG_DEBUG:
            for c in self.portage_cfg:
                print("%-32.32s = %-s" % (c, self.portage_cfg[c]))

        jobs = multiprocessing.cpu_count()
        self.emerge_default_opts = {
            "--buildpkg=y",
            "--usepkg=y",
            "--keep-going=y",
            "--jobs=" + str(jobs),
            "--load-average=" + str(jobs - 1),
            "--nospinner",
        }
        if "build-script" in ARG_DEBUG:
            self.emerge_default_opts |= {"--verbose"}
            self.emerge_default_opts |= {"--quiet=n"}
            self.emerge_default_opts |= {"--quiet-fail=n"}
        else:
            self.emerge_default_opts |= {"--quiet-build"}
            self.emerge_default_opts |= {"--quiet"}

        self.EMERGE_UPDATE_OPTS = {
            "--update",
            "--tree",
            "--changed-use",
            "--newuse",
            "--deep",
            "--with-bdeps=y",
            "--complete-graph-if-new-use=y",
        }

        # 'systemd' is hard dependancy?
        # 'git' for sync-type=git
        # 'symlink' for kernel
        self.portage_use_flags = {
            "systemd",
            "initramfs",
            "git",
            "symlink",
            "sdl",
            "-filecaps",
            "-savedconfig",
            "-split-bin",
            "-split-sbin",
            "-split-usr",
        }

        # -user* are required for access to USER_CONFIG_PATH
        # -pid-sandbox is required for cross compile scenarios
        self.portage_features = {
            "-userfetch",
            "-userpriv",
            "-usersync",
            "-usersandbox",
            "-sandbox",
            "-pid-sandbox",
            "-network-sandbox",
            "parallel-install",
            "buildpkg",
            "binpkg-multi-instance",
            "-binpkg-docompress",
            "getbinpkg",
            "-candy",
        }
        # TODO: portage_features.add("ccache")
        os.environ["FEATURES"] = " ".join(self.portage_features)
        os.environ["BOOTSTRAP_USE"] = " ".join(self.portage_use_flags)
        os.environ["USE"] = " ".join(self.portage_use_flags)
        os.environ["EGIT_CLONE_TYPE"] = "shallow"

        self.ARCHITECTURES = {
            "x86_64": ("amd64", "arch/x86/boot/bzImage"),
            "aarch64": ("arm64", "arch/arm64/boot/Image.gz"),
            "armv7l": ("arm", "arch/arm/boot/zImage"),
        }
        global kimg_path
        if args.architecture:
            self.arch, kimg_path = self.ARCHITECTURES[args.architecture]
        else:
            self.arch, kimg_path = self.ARCHITECTURES["x86_64"]

        # GENTOO_UPSTREAM : we only support systemd profiles! and only the no-multilib flaivour , for now;
        # GENTOO_UPSTREAM : wait for fix upstream: https://bugs.gentoo.org/792081
        # GENTOO_TODO     : add args.profile switch for this? (multilib vs. nomultilib)
        self.arch_profile = os.path.join("profiles/default/linux", self.arch, args.release, "no-multilib/systemd")

        self.pkgs_sys = {"@world"}

        self.pkgs_fs = {"sys-fs/dosfstools"}
        if args.output_format in (OutputFormat.subvolume, OutputFormat.gpt_btrfs):
            self.pkgs_fs.add("sys-fs/btrfs-progs")
        elif args.output_format == OutputFormat.gpt_xfs:
            self.pkgs_fs.add("sys-fs/xfsprogs")
        elif args.output_format == OutputFormat.gpt_squashfs:
            self.pkgs_fs.add("sys-fs/squashfs-tools")

        if args.encrypt:
            self.pkgs_fs.add("cryptsetup")
            self.pkgs_fs.add("device-mapper")

        self.grub_platforms = set()
        if not do_run_build_script and args.bootable:
            if args.esp_partno:
                self.pkgs_boot = [{"sys-kernel/installkernel-systemd-boot"}]
            elif args.bios_partno:
                self.pkgs_boot = [{"sys-boot/grub"}]
                self.grub_platforms = {"coreboot", "qemu", "pc"}
                os.environ["GRUB_PLATFORMS"] = " ".join(self.grub_platforms)

            self.pkgs_boot.append(
                {
                    "sys-kernel/gentoo-kernel-bin",
                    "sys-firmware/edk2-ovmf",
                    # "sys-kernel/linux-firmware",
                }
            )
        self.fetch_fix_stage3(root)
        self.set_profile(args, root)
        self.set_default_repo(root)
        self.unmask_arch(root)
        self.whitelist_licenses(root)
        self.provide_patches(args, root)
        self.set_useflags(root)
        self.gentoo_mkosi_conf(root)
        self.invoke_emerge(args, root, inside_stage3=False, actions={"--sync"})
        self.baselayout(args, root)
        self.update_stage3(args, root)

        if "build-script" in ARG_DEBUG:
            self.invoke_emerge(args, root, actions={"--info"})

    def fetch_fix_stage3(self, root: str) -> None:
        """usrmerge tracker bug: https://bugs.gentoo.org/690294"""

        # e.g.: http://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt
        stage3tsf_path_url = urllib.parse.urljoin(
            self.portage_cfg["GENTOO_MIRRORS"].partition(" ")[0],
            f"releases/{self.arch}/autobuilds/latest-stage3.txt",
        )
        stage3_tar = ""
        # GENTOO_UPSTREAM: wait for fix upstream: https://bugs.gentoo.org/792081
        # and more... so we can gladly escape all this hideousness!
        with urllib.request.urlopen(stage3tsf_path_url) as r:
            # 20210323T005051Z/stage3-arm64-systemd-20210323T005051Z.tar.xz 196362344
            # 20210711T170538Z/stage3-amd64-nomultilib-systemd-20210711T170538Z.tar.xz 214470580
            args_profile = "nomultilib"
            profilereg = re.compile(f"stage3-{self.arch}-{args_profile}-systemd")
            lines = list(r)
            for line in lines:
                l = line.decode("utf-8")
                if profilereg.search(l):
                    stage3_tar, _, _ = l.partition(" ")
            if not stage3_tar:
                die("profile names changed upstream?")

        stage3_url_path = urllib.parse.urljoin(
            self.portage_cfg["GENTOO_MIRRORS"],
            f"releases/{self.arch}/autobuilds/{stage3_tar}",
        )
        stage3_tar_path = os.path.join(self.portage_cfg["DISTDIR"], stage3_tar)
        if not os.path.isfile(stage3_tar_path):
            MkosiPrinter.print_step(f"Fetching {stage3_url_path}")
            os.makedirs(os.path.dirname(stage3_tar_path), exist_ok=True)
            urllib.request.urlretrieve(stage3_url_path, stage3_tar_path)

        with tarfile.open(stage3_tar_path) as tfd:
            MkosiPrinter.print_step(f"Extracting {os.path.basename(stage3_tar)}")
            # GENTOO: usrmerge mess
            bins = []
            usr = []
            usrreg = re.compile("^[.][/](etc|usr|var)")
            binreg = re.compile("^[.][/](sbin|bin|lib|lib64)")
            devreg = re.compile("^[.][/]dev")
            runreg = re.compile("^[.][/]run")

            members = tfd.getmembers()
            usrpath = os.path.join(root, "usr")

            self.UNINSTALL_IGNORE = {
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
            }

            ignore = ["./var/run", "./var/lock", "./bin/awk"]
            for dir in self.UNINSTALL_IGNORE:
                ignore += [dir] + [os.path.join("./usr", dir)]

            for tarinfo in members:
                if tarinfo.name in ignore or devreg.match(tarinfo.name) or runreg.match(tarinfo.name):
                    if "build-script" in ARG_DEBUG:
                        print("%32.32s -> /dev/null" % (tarinfo.name))
                    continue
                elif (
                    tarinfo.issym()
                    and (os.path.dirname(tarinfo.name) == "./usr/bin")
                    and (os.path.dirname(tarinfo.linkname) == "../../bin")
                ):
                    if "build-script" in ARG_DEBUG:
                        print("%32.32s -> %-32.32s -> /dev/null" % (tarinfo.name, tarinfo.linkname))
                    continue
                elif usrreg.match(tarinfo.name):
                    usr.append(tarinfo)
                elif binreg.match(tarinfo.name):
                    bins.append(tarinfo)
                else:
                    print("%32.32s -> /dev/null" % (tarinfo.name))
                    continue

            try:
                tfd.extractall(usrpath, bins, numeric_owner=True)
                tfd.extractall(root, usr, numeric_owner=True)
            except FileExistsError as e:
                pass

    def set_profile(self, args: CommandLineArguments, root: str) -> None:
        # be careful not to overwrite user's profile, (via skeleton)
        profile_path = os.path.join(root, PROFILE_PATH)
        if not os.path.islink(profile_path):
            MkosiPrinter.print_step(f"{args.distribution} setting Profile")
            os.makedirs(os.path.join(root, USER_CONFIG_PATH), exist_ok=True)
            os.symlink(os.path.join(self.portage_cfg["PORTDIR"], self.arch_profile), profile_path)

    def set_default_repo(self, root: str) -> None:
        os.makedirs(os.path.join(root, USER_CONFIG_PATH, "repos.conf"), exist_ok=True)
        with open(os.path.join(root, USER_CONFIG_PATH, "repos.conf", "eselect-repo.conf"), "w") as f:
            f.write(
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

    def unmask_arch(self, root: str) -> None:
        package_accept_keywords = os.path.join(root, USER_CONFIG_PATH, "package.accept_keywords")
        os.makedirs(package_accept_keywords, exist_ok=True)
        with open(os.path.join(package_accept_keywords, "mkosi"), "a") as f:
            # homed is still ~ARCH
            f.write(
                dedent(
                    f"""\
                    sys-auth/pambase ~{self.arch}
                    # sys-kernel/gentoo-kernel-bin ~{self.arch}
                    # virtual/dist-kernel ~{self.arch}
                    sys-apps/baselayout ~{self.arch}
                    """
                )
            )
        with open(os.path.join(package_accept_keywords, "bug765208"), "a") as f:
            f.write(dedent(f"""={self.dracut_atom}-053 ~{self.arch}"""))

    def whitelist_licenses(self, root: str) -> None:
        package_license = os.path.join(root, USER_CONFIG_PATH, "package.license")
        with open(package_license, "a") as f:
            f.write("sys-kernel/linux-firmware @BINARY-REDISTRIBUTABLE")

    def provide_patches(self, args: CommandLineArguments, root: str) -> None:
        MkosiPrinter.print_step(f"{args.distribution}: patching dracut [https://bugs.gentoo.org/765208]")
        patchdir = os.path.join(root, USER_CONFIG_PATH, "patch")
        os.makedirs(os.path.join(patchdir, self.dracut_atom), exist_ok=True)
        bug765208 = os.path.join(patchdir, self.dracut_atom, "bug765208.patch")
        dracut_path_url = "https://765208.bugs.gentoo.org/attachment.cgi?id=683770"
        urllib.request.urlretrieve(dracut_path_url, bug765208)

    def set_useflags(self, root: str) -> None:
        os.makedirs(os.path.join(root, CUSTOM_PROFILE_PATH), exist_ok=True)
        with open(os.path.join(root, CUSTOM_PROFILE_PATH, "use.force"), "w") as f:
            f.write(
                dedent(
                    """\
                    -split-bin
                    -split-sbin
                    -split-usr
                    """
                )
            )

        self.package_use = os.path.join(root, USER_CONFIG_PATH, "package.use")
        os.makedirs(self.package_use, exist_ok=True)

        with open(os.path.join(self.package_use, "baselayout"), "a") as f:
            f.write("sys-apps/baselayout build")

        with open(os.path.join(self.package_use, "systemd"), "a") as f:
            # repart for usronly
            f.write(
                dedent(
                    f"""\
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
        with open(os.path.join(self.package_use, "grub"), "a") as f:
            # repart for usronly
            f.write(
                dedent(
                    f"""\
                    sys-boot/grub device-mapper truetype
                    """
                )
            )

    def gentoo_mkosi_conf(self, root: str) -> None:
        package_env = os.path.join(root, USER_CONFIG_PATH, "package.env")
        ebuild_sh_env_dir = os.path.join(root, EBUILD_SH_ENV_DIR)
        os.makedirs(package_env, exist_ok=True)
        os.makedirs(ebuild_sh_env_dir, exist_ok=True)

        # we use this so we don't need to touch upstream files
        # we also use this for documenting build environment as much as possible
        gentoo_mkosi_conf = os.path.join(ebuild_sh_env_dir, "mkosi.conf")
        # apply whatever we put in gentoo_mkosi_conf to runs invokation of emerge
        with open(os.path.join(package_env, "mkosi"), "a") as f:
            f.write(f"*/*    mkosi.conf\n")

        uninstall_ignore = 'UNINSTALL_IGNORE="${UNINSTALL_IGNORE} ' + " ".join(self.UNINSTALL_IGNORE) + '"'
        use = 'USE="${USE} ' + " ".join(self.portage_use_flags) + '"'
        with open(gentoo_mkosi_conf, "a") as f:
            f.write(
                dedent(
                    f"""\
                    # MKOSI: these were used during image creation...
                    # and some more! see under package.*/
                    #
                    # usrmerge (see all under profile/)
                    {uninstall_ignore}
                    {use}
                    """
                )
            )

    def invoke_emerge(
        self,
        args: CommandLineArguments,
        root: str,
        inside_stage3: bool = True,
        pkgs: Set[str] = set(),
        actions: Set[str] = set(),
        opts: Set[str] = set(),
    ) -> None:
        if not inside_stage3:
            from _emerge.main import emerge_main  # type: ignore

            os.environ["FEATURES"] = " ".join(self.portage_features)
            os.environ["BOOTSTRAP_USE"] = " ".join(self.portage_use_flags)
            os.environ["USE"] = " ".join(self.portage_use_flags)
            os.environ["GRUB_PLATFORMS"] = " ".join(self.grub_platforms)
            os.environ["EGIT_CLONE_TYPE"] = "shallow"

            PREFIX_OPTS: Set[str] = set()
            if "--sync" not in actions:
                PREFIX_OPTS = {
                    "--config-root=" + root,
                    "--root=" + root,
                    "--sysroot=" + root,
                }

            emerge_main(pkgs.union(opts, PREFIX_OPTS, self.emerge_default_opts, actions))
        else:
            cmdline = ["/usr/bin/emerge"]

            emerge_env = {
                "FEATURES": " ".join(self.portage_features),
                "BOOTSTRAP_USE": " ".join(self.portage_use_flags),
                "USE": " ".join(self.portage_use_flags),
                "GRUB_PLATFORMS": " ".join(self.grub_platforms),
                "EGIT_CLONE_TYPE": "shallow",
            }

            cmdline.extend(pkgs)
            cmdline.extend(self.emerge_default_opts)
            cmdline.extend(opts)
            cmdline.extend(actions)

            MkosiPrinter.print_step(f"Invoking emerg(1) inside stage3")
            run_workspace_command(
                args,
                root,
                cmdline,
                network=True,
                env=emerge_env,
                nspawn_params=[
                    "--capability=CAP_SYS_ADMIN,CAP_MKNOD",
                    "--bind=" + self.portage_cfg["PORTDIR"],
                    "--bind=" + self.portage_cfg["DISTDIR"],
                    "--bind=" + self.portage_cfg["PKGDIR"],
                    "--bind=" + "/sys",
                ],
            )

    def baselayout(self, args: CommandLineArguments, root: str) -> None:
        # TOTHINK: sticky bizness when when image profile != host profile
        self.invoke_emerge(args, root, inside_stage3=True, pkgs={"sys-apps/baselayout"})
        cmdline = ["env-update"]
        run_workspace_command(
            args,
            root,
            cmdline,
            nspawn_params=[
                "--capability=CAP_SYS_ADMIN,CAP_MKNOD",
                "--bind=" + self.portage_cfg["PORTDIR"],
                "--bind=" + self.portage_cfg["DISTDIR"],
                "--bind=" + self.portage_cfg["PKGDIR"],
            ],
        )

    def update_stage3(self, args: CommandLineArguments, root: str) -> None:
        # baselayout gets remerged here because of USE=build which overrides
        # /etc/os-release; (see below)
        self.invoke_emerge(args, root, pkgs=self.pkgs_sys, opts=self.EMERGE_UPDATE_OPTS)

        # DONTMOVE: baselayout is settled down by now, dracut needs this for
        # version_id in /etc/os-release data (UKI)
        # GENTOO_BUG: https://bugs.gentoo.org/788190
        with open(os.path.join(root, "etc/os-release"), "a") as f:
            f.write(f"VERSION_ID=args.release\n")

        # it's a possibility that updating @world will update
        # openssh/baselaout... so do these cleanups at the end
        os.unlink(os.path.join(self.package_use, "baselayout"))
        # FIXME?: without this we get the following
        # Synchronizing state of sshd.service with SysV service script with /lib/systemd/systemd-sysv-install.
        # Executing: /lib/systemd/systemd-sysv-install --root=/var/tmp/mkosi-2b6snh_u/root enable sshd
        # chroot: failed to run command ‘/usr/sbin/update-rc.d’: No such file or directory
        os.unlink(os.path.join(root, "etc/init.d/sshd"))

    def _dbg(self, args: CommandLineArguments, root: str) -> None:
        """this is for dropping into shell to see what's wrong"""

        cmdline = ["/bin/sh"]
        run_workspace_command(
            args,
            root,
            cmdline,
            nspawn_params=[
                "--capability=CAP_SYS_ADMIN,CAP_MKNOD",
                "--bind=" + self.portage_cfg["PORTDIR"],
                "--bind=" + self.portage_cfg["DISTDIR"],
                "--bind=" + self.portage_cfg["PKGDIR"],
            ],
        )
