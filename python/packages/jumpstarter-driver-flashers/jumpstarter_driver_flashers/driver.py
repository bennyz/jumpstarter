import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import anyio.to_thread
import oras.defaults
from jumpstarter_driver_composite.driver import Proxy
from jumpstarter_driver_http.driver import HttpServer
from jumpstarter_driver_tftp.driver import Tftp
from jumpstarter_driver_uboot.driver import UbootConsole
from oras.container import Container
from oras.provider import Registry

from .bundle import FlasherBundleManifestV1Alpha1
from jumpstarter.common.exceptions import ConfigurationError
from jumpstarter.driver import Driver, export

# Where the exporter image installs the aarch64 fls build served to the DUT
# (see python/Containerfile), in order of preference.
FLS_BINARY_PATHS = (Path("/usr/local/bin/fls-aarch64"), Path("/usr/local/bin/fls"))

# Name prefix of the directories bundles are pulled into before they are published
STAGING_PREFIX = ".pull-"


def _manifest_digest(oras_client: Registry, container: Container) -> str:
    """Return the digest of the manifest the registry serves for the container

    get_manifest() returns only the parsed JSON, and the digest covers the exact bytes.
    """
    oras_client.auth.load_configs(container)
    response = oras_client.do_request(
        f"{oras_client.prefix}://{container.manifest_url()}",
        headers={"Accept": oras.defaults.default_manifest_media_type},
    )
    response.raise_for_status()
    return f"sha256:{hashlib.sha256(response.content).hexdigest()}"


def _pull_and_publish(oras_client: Registry, reference: str, bundle_dir: Path) -> None:
    """Pull into a staging directory and publish it as bundle_dir with one rename

    A published directory holds a complete pull and is never written again, so
    a pull that fails halfway, or runs beside another session's, cannot damage
    what other sessions read.
    """
    bundle_root = bundle_dir.parent
    bundle_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=bundle_root, prefix=STAGING_PREFIX) as staging:
        pulled = Path(staging) / "bundle"
        pulled.mkdir()
        oras_client.pull(reference, outdir=str(pulled))
        try:
            pulled.rename(bundle_dir)
        except OSError:
            # another session published the same digest first: keep theirs
            if not bundle_dir.is_dir():
                raise

    # Drop older versions of the bundle, and the files of the flat layout older
    # releases pulled into bundle_root itself. A session on another board still
    # reading an older version loses it, which takes a tag move during its flash.
    for entry in bundle_root.iterdir():
        if entry == bundle_dir or entry.name.startswith(STAGING_PREFIX):
            continue
        if entry.is_dir():
            # another session publishing this digest may be removing it too
            shutil.rmtree(entry, onexc=_ignore_missing)
        else:
            entry.unlink(missing_ok=True)


def _ignore_missing(function, path, exc: BaseException) -> None:
    if not isinstance(exc, FileNotFoundError):
        raise exc


@dataclass(kw_only=True)
class BaseFlasher(Driver):
    """driver for Jumpstarter"""

    driver_type = "storage"

    flasher_bundle: str = field(default="quay.io/jumpstarter-dev/jumpstarter-flasher-test:latest")
    variant: None | str = field(default=None)
    manifest: str = field(default="manifest.yaml")
    default_target: None | str = field(default=None)
    cache_dir: str = field(default="/var/lib/jumpstarter/flasher")
    tftp_dir: str = field(default="/var/lib/tftpboot")
    http_dir: str = field(default="/var/www/html")
    cacert: str | None = field(default=None)
    exporter_ip: str | None = field(default=None)
    # fls version the DUT downloads from GitHub releases. None: serve the fls
    # binary installed in the exporter image instead (no GitHub access needed).
    fls_version: str | None = field(default=None)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        # Ensure required children are present if not already instantiated
        # in configuration. When exporter_ip is configured, the auto-created
        # children advertise it to the DUT without binding to it.
        if "tftp" not in self.children:
            self.children["tftp"] = Tftp(
                root_dir=self.tftp_dir,
                remove_created_on_close=True,
                advertised_host=self.exporter_ip,
            )
        if "http" not in self.children:
            self.children["http"] = HttpServer(
                root_dir=self.http_dir,
                remove_created_on_close=True,
                advertised_host=self.exporter_ip,
            )

        # A ref: child is a Proxy that only resolves at enumerate(), so check it on use.
        # Check direct children now, so a wrong type fails when the exporter starts.
        for name, cls in (("tftp", Tftp), ("http", HttpServer)):
            if not isinstance(self.children[name], Proxy):
                self._child(name, cls)

        # Ensure required children are present, the following are not auto-created
        if "serial" not in self.children:
            raise ConfigurationError(
                "'serial' instance is required for BaseFlasher either via a ref ir a direct child instance"
            )

        if "power" not in self.children:
            raise ConfigurationError(
                "'power' instance is required for BaseFlasher either via a ref ir a direct child instance"
            )

        if "uboot" not in self.children:
            self.children["uboot"] = UbootConsole(
                children={
                    "power": self.children["power"],
                    "serial": self.children["serial"],
                }
            )

        # bundles already checked against the registry in the current session
        self._downloaded: dict[str, Path] = {}

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_flashers.client.BaseFlasherClient"

    def _child[T: Driver](self, name: str, cls: type[T]) -> T:
        """Return child `name`, following a ref:, checked to be a `cls`"""
        child = self.children[name]
        if isinstance(child, Proxy):
            child = child.target
        if not isinstance(child, cls):
            raise ConfigurationError(
                f"'{name}' child of BaseFlasher must be a {cls.__name__} driver, got {type(child).__name__}"
            )
        return child

    @property
    def tftp(self) -> Tftp:
        return self._child("tftp", Tftp)

    @property
    def http(self) -> HttpServer:
        return self._child("http", HttpServer)

    @export
    async def get_default_target(self):
        """Return the default target"""
        return self.default_target

    @export
    async def get_cacert(self) -> str | None:
        """Return the CA certificate contents if configured"""
        if not self.cacert:
            return None
        with open(self.cacert) as f:  # noqa: ASYNC230
            return f.read()

    @export
    async def get_fls_version(self) -> str | None:
        """Return the fls version to download from GitHub, or None to serve the exporter's own binary"""
        return self.fls_version or None

    @export
    async def setup_fls_binary(self):
        """Copy local fls binary to HTTP server root for DUT download"""
        fls_path = next((p for p in FLS_BINARY_PATHS if p.exists()), None)
        if fls_path is None:
            raise ConfigurationError(f"fls binary not found at {' or '.join(str(p) for p in FLS_BINARY_PATHS)}")
        await self.http.storage.copy_exporter_file(fls_path, "fls")

    @export
    async def setup_flasher_bundle(self, force_flash_bundle: str | None = None):
        """Setup flasher bundle

        This method sets all the files in place in the tftp server
        so that the target can download from bootloader.
        """

        # the client is requesting a different flasher bundle
        if force_flash_bundle:
            self.flasher_bundle = force_flash_bundle

        manifest = await self.get_flasher_manifest()
        kernel_path = await self._get_file_path(manifest.spec.kernel.file)
        self.logger.info(f"Setting up kernel in tftp: {kernel_path}")
        await self.tftp.storage.copy_exporter_file(kernel_path, kernel_path.name)

        initram_file = manifest.get_initram_file()
        if initram_file:
            initram_path = await self._get_file_path(initram_file)
            self.logger.info(f"Setting up initram in tftp: {initram_path}")
            await self.tftp.storage.copy_exporter_file(initram_path, initram_path.name)

        dtb_file = manifest.get_dtb_file(self.variant) if manifest.spec.dtb else None
        if dtb_file:
            dtb_path = await self._get_file_path(dtb_file)
            self.logger.info(f"Setting up dtb in tftp: {dtb_path}")
            await self.tftp.storage.copy_exporter_file(dtb_path, dtb_path.name)

    @export
    def set_dtb(self, handle):
        """Provide a different dtb from client"""
        raise NotImplementedError

    @export
    async def use_dtb_variant(self, variant):
        """Provide a different dtb reference from the flasher bundle"""
        manifest = await self.get_flasher_manifest()
        # Check if the variant exists in the manifest
        if not manifest.spec.dtb or variant not in manifest.spec.dtb.variants:
            variant_list = []
            if manifest.spec.dtb:
                variant_list = list(manifest.spec.dtb.variants.keys())
            raise ValueError(
                f"DTB variant {variant} not found in the flasher bundle, available variants are: {variant_list}."
            )
        self.variant = variant

    def set_kernel(self, handle):
        """Provide a different kernel from client"""
        raise NotImplementedError

    def set_initram(self, handle):
        """Provide a different initram from client"""
        raise NotImplementedError

    def _download_to_cache(self, bundle: str) -> Path:
        """Download the bundle to the cache

        The cache outlives the session and keeps the bundle under its manifest
        digest, so it is pulled again only when its tag moves to another
        manifest. Within a session the registry is asked once.
        """
        if bundle in self._downloaded:
            self.logger.debug(f"Bundle already downloaded: {bundle}")
            return self._downloaded[bundle]

        oras_client = Registry()
        container = oras_client.get_container(bundle)
        digest = _manifest_digest(oras_client, container)

        # make a filesystem valid name for the cache directory
        bundle_root = Path(self.cache_dir) / bundle.replace(":", "_").replace("/", "_")
        bundle_dir = bundle_root / digest.replace(":", "_")
        if bundle_dir.is_dir():
            self.logger.info(f"Using cached bundle {bundle} ({digest}) in {bundle_dir}")
        else:
            self.logger.info(f"Downloading bundle: {bundle} ({digest})")
            # pull by digest, so the files are the ones the digest names even if the tag moves meanwhile
            _pull_and_publish(oras_client, f"{container.registry}/{container.api_prefix}@{digest}", bundle_dir)
            self.logger.info(f"Bundle downloaded to {bundle_dir}")

        self._downloaded[bundle] = bundle_dir
        return bundle_dir

    async def _get_file_path(self, filename) -> Path:
        """Get the bundle contents path.

        This function will ensure that the bundle is downloaded into cache, and
        then return the path to the requested file in the cache directory.
        """
        if filename is None:
            raise ValueError("filename cannot be None")
        # read the bundle once here: setup_flasher_bundle can change it while the worker runs
        bundle_dir = await anyio.to_thread.run_sync(self._download_to_cache, self.flasher_bundle)
        return bundle_dir / filename

    @export
    async def get_flasher_manifest_yaml(self) -> str:
        """Return the manifest yaml as a string for client side consumption"""
        with open(await self._get_file_path(self.manifest)) as f:  # noqa: ASYNC230
            return f.read()

    async def get_flasher_manifest(self) -> FlasherBundleManifestV1Alpha1:
        filename = await self._get_file_path(self.manifest)
        return FlasherBundleManifestV1Alpha1.from_file(filename)

    @export
    async def get_kernel_filename(self) -> str:
        """Return the kernel filename"""
        manifest = await self.get_flasher_manifest()
        return Path(manifest.get_kernel_file()).name

    @export
    async def get_initram_filename(self) -> str | None:
        """Return the initram filename"""
        manifest = await self.get_flasher_manifest()
        filename = manifest.get_initram_file()
        if filename:
            return Path(filename).name

    @export
    async def get_dtb_filename(self) -> str:
        """Return the dtb filename"""
        manifest = await self.get_flasher_manifest()
        dtb_file = manifest.get_dtb_file(self.variant)
        if dtb_file:
            return Path(dtb_file).name
        else:
            return ""

    @export
    async def get_dtb_address(self) -> str | None:
        """Return the dtb address"""
        manifest = await self.get_flasher_manifest()
        return manifest.get_dtb_address()

    @export
    async def get_kernel_address(self) -> str:
        """Return the kernel address"""
        manifest = await self.get_flasher_manifest()
        return manifest.get_kernel_address()

    @export
    async def get_initram_address(self) -> str | None:
        """Return the initram address"""
        manifest = await self.get_flasher_manifest()
        return manifest.get_initram_address()

    @export
    async def get_bootcmd(self) -> str:
        """Return the bootcmd"""
        manifest = await self.get_flasher_manifest()
        return manifest.get_boot_cmd(self.variant)


@dataclass(kw_only=True)
class TIJ784S4Flasher(BaseFlasher):
    """driver for Jumpstarter"""

    flasher_bundle: str = "quay.io/jumpstarter-dev/jumpstarter-flasher-ti-j784s4:latest"


@dataclass(kw_only=True)
class RCarS4Flasher(BaseFlasher):
    """RCarS4 driver for Jumpstarter"""

    flasher_bundle: str = "quay.io/jumpstarter-dev/jumpstarter-flasher-rcar-s4:latest"
