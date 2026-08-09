"""Strict durable address aliases and GET-only package recipes.

The repository persists only domain-separated provider identity digests, one
strict store slug needed for exact lookup, and display-safe labels.  Durable
records never contain live handles, full addresses, coordinates, generations,
prices, basket/quote/payment data, or mutation authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from .ordering_contracts import (
    AddressSnapshot,
    CatalogOption,
    CatalogOptionGroup,
    CatalogProduct,
    CustomerIdentity,
    LiveStore,
)

STORE_VERSION: Final = 1
STORE_KEY_PREFIX: Final = "glovo.ordering_packages_v1"
MAX_SERIALIZED_BYTES: Final = 512 * 1024
MAX_DEPTH: Final = 12
MAX_REVISION: Final = 2_147_483_647
MAX_ADDRESSES: Final = 64
MAX_PACKAGES: Final = 100
MAX_ALIASES: Final = 8
MAX_ITEMS: Final = 50
MAX_TOTAL_QUANTITY: Final = 100
MAX_GROUPS: Final = 24
MAX_OPTIONS: Final = 64
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ADDRESS_REF_RE = re.compile(r"^addr-[0-9a-f]{32}$")
_PACKAGE_REF_RE = re.compile(r"^pkg-[0-9a-f]{32}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_ALIAS_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
STALE_REASONS: Final = frozenset(
    {
        "account_changed",
        "address_missing_or_changed",
        "store_missing_or_changed",
        "product_missing_or_changed",
        "option_missing_or_changed",
        "selection_constraints_changed",
    }
)


class PackageLibraryError(ValueError):
    """Stable, redaction-safe library failure."""

    def __init__(self) -> None:
        super().__init__("ordering library is unavailable or invalid")


class PackageLibraryUnavailable(PackageLibraryError):
    """Persistence is corrupt or a save outcome cannot be trusted."""


class PackageStale(PackageLibraryError):
    """An exact private recipe no longer reconciles."""

    def __init__(self, reason: str) -> None:
        if reason not in STALE_REASONS:
            raise PackageLibraryError
        self.reason = reason
        super().__init__()


def _strict_int(value: object, *, minimum: int, maximum: int = MAX_REVISION) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PackageLibraryError
    return value


def _normalized_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise PackageLibraryError
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not 1 <= len(value) <= maximum or any(unicodedata.category(char).startswith("C") for char in value):
        raise PackageLibraryError
    return value


def normalize_address_name(value: object) -> str:
    """Return the one canonical address-alias spelling accepted everywhere."""
    name = _normalized_text(value, maximum=40)
    normalize_lookup_key(name)
    return name


def normalize_package_name(value: object) -> str:
    name = _normalized_text(value, maximum=48)
    normalize_lookup_key(name)
    return name


def normalize_lookup_key(value: object) -> str:
    value = _normalized_text(value, maximum=48).casefold()
    value = re.sub(r"[\s_-]+", "-", value)
    if _ALIAS_KEY_RE.fullmatch(value) is None:
        raise PackageLibraryError
    return value


def normalize_aliases(value: object, *, package_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ALIASES:
        raise PackageLibraryError
    aliases = tuple(normalize_package_name(item) for item in value)
    keys = [normalize_lookup_key(item) for item in aliases]
    if len(set(keys)) != len(keys) or normalize_lookup_key(package_name) in keys:
        raise PackageLibraryError
    return aliases


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise PackageLibraryError
    return value


def _domain_digest(kind: str, parts: Sequence[object]) -> str:
    encoded = json.dumps(
        {"version": 1, "kind": kind, "parts": list(parts)},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def account_digest(customer: CustomerIdentity) -> str:
    if not isinstance(customer, CustomerIdentity):
        raise PackageLibraryError
    return _domain_digest("account", (customer.customer_id,))


def address_digest(address: AddressSnapshot) -> str:
    if not isinstance(address, AddressSnapshot):
        raise PackageLibraryError
    if not math.isfinite(address.latitude) or not math.isfinite(address.longitude):
        raise PackageLibraryError
    fields = sorted((field.field_type, field.value) for field in address.fields)
    return _domain_digest(
        "address",
        (
            address.remote_id,
            address.address_line,
            address.details,
            address.country_code,
            address.city_code,
            address.city_name,
            address.kind,
            address.tag,
            address.latitude,
            address.longitude,
            fields,
        ),
    )


def store_digest(store: LiveStore) -> str:
    if not isinstance(store, LiveStore):
        raise PackageLibraryError
    return _domain_digest(
        "store",
        (store.slug, store.store_id, store.address_id, store.category_id, store.city_code),
    )


def product_digest(product: CatalogProduct) -> str:
    if not isinstance(product, CatalogProduct):
        raise PackageLibraryError
    return _domain_digest(
        "product", (product.product_id, product.external_id, product.store_product_id)
    )


def group_digest(group: CatalogOptionGroup) -> str:
    if not isinstance(group, CatalogOptionGroup):
        raise PackageLibraryError
    return _domain_digest("group", (group.key, group.external_id, group.position))


def option_digest(group: CatalogOptionGroup, option: CatalogOption) -> str:
    if not isinstance(group, CatalogOptionGroup) or not isinstance(option, CatalogOption):
        raise PackageLibraryError
    return _domain_digest(
        "option", (group.key, group.external_id, option.key, option.external_id)
    )


@dataclass(frozen=True, slots=True, repr=False)
class AddressAlias:
    alias_ref: str
    revision: int
    name: str
    match_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.alias_ref, str) or _ADDRESS_REF_RE.fullmatch(self.alias_ref) is None:
            raise PackageLibraryError
        object.__setattr__(self, "revision", _strict_int(self.revision, minimum=1))
        object.__setattr__(self, "name", normalize_address_name(self.name))
        object.__setattr__(self, "match_digest", _digest(self.match_digest))

    def public_dict(self) -> dict[str, Any]:
        return {"addressRef": self.alias_ref, "revision": self.revision, "name": self.name}

    def storage_dict(self) -> dict[str, Any]:
        return {
            "alias_ref": self.alias_ref,
            "revision": self.revision,
            "name": self.name,
            "match_digest": self.match_digest,
        }


@dataclass(frozen=True, slots=True, repr=False)
class RecipeGroup:
    group_digest: str
    label: str
    option_digests: tuple[str, ...]
    option_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_digest", _digest(self.group_digest))
        object.__setattr__(self, "label", _normalized_text(self.label, maximum=100))
        if (
            not isinstance(self.option_digests, tuple)
            or not isinstance(self.option_labels, tuple)
            or len(self.option_digests) != len(self.option_labels)
            or len(self.option_digests) > MAX_OPTIONS
            or len(set(self.option_digests)) != len(self.option_digests)
        ):
            raise PackageLibraryError
        for digest in self.option_digests:
            _digest(digest)
        for label in self.option_labels:
            _normalized_text(label, maximum=100)

    def storage_dict(self) -> dict[str, Any]:
        return {
            "group_digest": self.group_digest,
            "label": self.label,
            "option_digests": list(self.option_digests),
            "option_labels": list(self.option_labels),
        }


@dataclass(frozen=True, slots=True, repr=False)
class RecipeItem:
    product_digest: str
    label: str
    quantity: int
    option_groups: tuple[RecipeGroup, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_digest", _digest(self.product_digest))
        object.__setattr__(self, "label", _normalized_text(self.label, maximum=120))
        object.__setattr__(self, "quantity", _strict_int(self.quantity, minimum=1, maximum=50))
        if (
            not isinstance(self.option_groups, tuple)
            or len(self.option_groups) > MAX_GROUPS
            or not all(isinstance(item, RecipeGroup) for item in self.option_groups)
            or len({item.group_digest for item in self.option_groups}) != len(self.option_groups)
        ):
            raise PackageLibraryError

    def public_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "quantity": self.quantity,
            "options": [label for group in self.option_groups for label in group.option_labels],
        }

    def storage_dict(self) -> dict[str, Any]:
        return {
            "product_digest": self.product_digest,
            "label": self.label,
            "quantity": self.quantity,
            "option_groups": [item.storage_dict() for item in self.option_groups],
        }


@dataclass(frozen=True, slots=True, repr=False)
class PackageRecord:
    package_ref: str
    revision: int
    name: str
    aliases: tuple[str, ...]
    address_ref: str
    address_revision: int
    store_slug: str
    store_digest: str
    store_label: str
    items: tuple[RecipeItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.package_ref, str) or _PACKAGE_REF_RE.fullmatch(self.package_ref) is None:
            raise PackageLibraryError
        object.__setattr__(self, "revision", _strict_int(self.revision, minimum=1))
        name = normalize_package_name(self.name)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "aliases", normalize_aliases(self.aliases, package_name=name))
        if not isinstance(self.address_ref, str) or _ADDRESS_REF_RE.fullmatch(self.address_ref) is None:
            raise PackageLibraryError
        object.__setattr__(self, "address_revision", _strict_int(self.address_revision, minimum=1))
        if not isinstance(self.store_slug, str) or _SLUG_RE.fullmatch(self.store_slug) is None:
            raise PackageLibraryError
        object.__setattr__(self, "store_digest", _digest(self.store_digest))
        object.__setattr__(self, "store_label", _normalized_text(self.store_label, maximum=120))
        if (
            not isinstance(self.items, tuple)
            or not 1 <= len(self.items) <= MAX_ITEMS
            or not all(isinstance(item, RecipeItem) for item in self.items)
            or len({item.product_digest for item in self.items}) != len(self.items)
            or sum(item.quantity for item in self.items) > MAX_TOTAL_QUANTITY
        ):
            raise PackageLibraryError

    def public_dict(self, address: AddressAlias) -> dict[str, Any]:
        if not isinstance(address, AddressAlias):
            raise PackageLibraryError
        return {
            "packageRef": self.package_ref,
            "revision": self.revision,
            "name": self.name,
            "aliases": list(self.aliases),
            "storeLabel": self.store_label,
            "addressRef": self.address_ref,
            "addressName": address.name,
            "itemCount": sum(item.quantity for item in self.items),
            "items": [item.public_dict() for item in self.items],
        }

    def storage_dict(self) -> dict[str, Any]:
        return {
            "package_ref": self.package_ref,
            "revision": self.revision,
            "name": self.name,
            "aliases": list(self.aliases),
            "address_ref": self.address_ref,
            "address_revision": self.address_revision,
            "store_slug": self.store_slug,
            "store_digest": self.store_digest,
            "store_label": self.store_label,
            "items": [item.storage_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True, repr=False)
class LibraryImage:
    revision: int = 0
    account_digest: str | None = None
    addresses: tuple[AddressAlias, ...] = ()
    packages: tuple[PackageRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", _strict_int(self.revision, minimum=0))
        if self.account_digest is not None:
            object.__setattr__(self, "account_digest", _digest(self.account_digest))
        if (
            not isinstance(self.addresses, tuple)
            or len(self.addresses) > MAX_ADDRESSES
            or not all(isinstance(item, AddressAlias) for item in self.addresses)
            or len({item.alias_ref for item in self.addresses}) != len(self.addresses)
            or len({normalize_lookup_key(item.name) for item in self.addresses})
            != len(self.addresses)
            or not isinstance(self.packages, tuple)
            or len(self.packages) > MAX_PACKAGES
            or not all(isinstance(item, PackageRecord) for item in self.packages)
            or len({item.package_ref for item in self.packages}) != len(self.packages)
        ):
            raise PackageLibraryError
        tokens = [
            normalize_lookup_key(token)
            for item in self.packages
            for token in (item.name, *item.aliases)
        ]
        if len(set(tokens)) != len(tokens):
            raise PackageLibraryError
        address_refs = {item.alias_ref for item in self.addresses}
        if any(item.address_ref not in address_refs for item in self.packages):
            raise PackageLibraryError
        if (self.addresses or self.packages) and self.account_digest is None:
            raise PackageLibraryError

    def storage_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "revision": self.revision,
            "account_digest": self.account_digest,
            "address_aliases": [item.storage_dict() for item in self.addresses],
            "packages": [item.storage_dict() for item in self.packages],
        }


class PackageLibraryStorage(Protocol):
    async def async_load(self) -> Mapping[str, Any] | None: ...
    async def async_save(self, value: dict[str, Any]) -> None: ...


class MemoryPackageLibraryStorage:
    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        self.value = value

    async def async_load(self) -> Mapping[str, Any] | None:
        return self.value

    async def async_save(self, value: dict[str, Any]) -> None:
        self.value = json.loads(json.dumps(value, allow_nan=False))


class HomeAssistantPackageLibraryStorage:
    def __init__(self, hass: Any, entry_id: str) -> None:
        if not isinstance(entry_id, str) or not entry_id:
            raise PackageLibraryError
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, STORE_VERSION, f"{STORE_KEY_PREFIX}.{entry_id}")

    async def async_load(self) -> Mapping[str, Any] | None:
        return await self._store.async_load()

    async def async_save(self, value: dict[str, Any]) -> None:
        await self._store.async_save(value)


def _exact_mapping(value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise PackageLibraryError
    return value


def _depth(value: object, depth: int = 0) -> int:
    if depth > MAX_DEPTH:
        raise PackageLibraryError
    if isinstance(value, Mapping):
        return max((depth, *(_depth(child, depth + 1) for child in value.values())))
    if isinstance(value, (list, tuple)):
        return max((depth, *(_depth(child, depth + 1) for child in value)))
    return depth


def parse_library_image(value: object) -> LibraryImage:
    """Parse the entire strict v1 image; unknown keys and partial recovery fail."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, OverflowError):
        raise PackageLibraryError from None
    if len(encoded) > MAX_SERIALIZED_BYTES:
        raise PackageLibraryError
    _depth(value)
    root = _exact_mapping(
        value,
        {"version", "revision", "account_digest", "address_aliases", "packages"},
    )
    if root["version"] != STORE_VERSION or isinstance(root["version"], bool):
        raise PackageLibraryError
    raw_addresses = root["address_aliases"]
    raw_packages = root["packages"]
    if not isinstance(raw_addresses, list) or not isinstance(raw_packages, list):
        raise PackageLibraryError
    addresses: list[AddressAlias] = []
    for raw in raw_addresses:
        row = _exact_mapping(raw, {"alias_ref", "revision", "name", "match_digest"})
        addresses.append(AddressAlias(row["alias_ref"], row["revision"], row["name"], row["match_digest"]))
    packages: list[PackageRecord] = []
    for raw in raw_packages:
        row = _exact_mapping(
            raw,
            {
                "package_ref", "revision", "name", "aliases", "address_ref",
                "address_revision", "store_slug", "store_digest", "store_label", "items",
            },
        )
        if not isinstance(row["items"], list) or not isinstance(row["aliases"], list):
            raise PackageLibraryError
        items: list[RecipeItem] = []
        for raw_item in row["items"]:
            item = _exact_mapping(raw_item, {"product_digest", "label", "quantity", "option_groups"})
            if not isinstance(item["option_groups"], list):
                raise PackageLibraryError
            groups: list[RecipeGroup] = []
            for raw_group in item["option_groups"]:
                group = _exact_mapping(raw_group, {"group_digest", "label", "option_digests", "option_labels"})
                if not isinstance(group["option_digests"], list) or not isinstance(group["option_labels"], list):
                    raise PackageLibraryError
                groups.append(RecipeGroup(group["group_digest"], group["label"], tuple(group["option_digests"]), tuple(group["option_labels"])))
            items.append(RecipeItem(item["product_digest"], item["label"], item["quantity"], tuple(groups)))
        packages.append(
            PackageRecord(
                row["package_ref"], row["revision"], row["name"], tuple(row["aliases"]),
                row["address_ref"], row["address_revision"], row["store_slug"],
                row["store_digest"], row["store_label"], tuple(items),
            )
        )
    return LibraryImage(root["revision"], root["account_digest"], tuple(addresses), tuple(packages))


class PackageLibrary:
    """CAS repository with cancellation-safe save-before-publish semantics."""

    def __init__(
        self,
        storage: PackageLibraryStorage,
        *,
        ref_source: Callable[[str], str] | None = None,
    ) -> None:
        self._storage = storage
        self._ref_source = ref_source or (lambda prefix: f"{prefix}-{secrets.token_hex(16)}")
        self._image = LibraryImage()
        self._lock = asyncio.Lock()
        self._loaded = False
        self._writable = True

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def writable(self) -> bool:
        return self._loaded and self._writable

    @property
    def revision(self) -> int:
        return self._image.revision

    async def _async_save_candidate(self, candidate: LibraryImage) -> None:
        task = asyncio.create_task(self._storage.async_save(candidate.storage_dict()))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            task.result()
        except BaseException as err:
            self._writable = False
            if isinstance(err, (KeyboardInterrupt, SystemExit)):
                raise
            raise PackageLibraryUnavailable from None
        self._image = candidate
        if cancelled:
            raise asyncio.CancelledError

    async def async_load(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            candidate: LibraryImage | None = None
            saving_initial_image = False
            try:
                raw = await self._storage.async_load()
                candidate = LibraryImage() if raw is None else parse_library_image(raw)
                if raw is None:
                    saving_initial_image = True
                    await self._async_save_candidate(candidate)
            except asyncio.CancelledError:
                # A cancellation while reading leaves durable state unknown and
                # must not publish the constructor's empty default. If the read
                # proved storage empty and the cancellation-resistant initial
                # save completed, _async_save_candidate already published the
                # exact persisted candidate and it is safe to mark loaded.
                if (
                    saving_initial_image
                    and candidate is not None
                    and self._image is candidate
                ):
                    self._loaded = True
                else:
                    self._loaded = False
                raise
            except Exception:
                self._writable = False
                raise PackageLibraryUnavailable from None
            self._image = candidate
            self._loaded = True

    def _guard(self) -> None:
        if not self._loaded:
            raise PackageLibraryUnavailable

    def _write_guard(self, expected_revision: object) -> None:
        self._guard()
        if not self._writable or _strict_int(expected_revision, minimum=0) != self._image.revision:
            raise PackageLibraryUnavailable
        if self._image.revision >= MAX_REVISION:
            raise PackageLibraryUnavailable

    def _new_ref(self, prefix: str, existing: set[str]) -> str:
        pattern = _ADDRESS_REF_RE if prefix == "addr" else _PACKAGE_REF_RE
        for _ in range(8):
            value = self._ref_source(prefix)
            if isinstance(value, str) and pattern.fullmatch(value) and value not in existing:
                return value
        raise PackageLibraryUnavailable

    @staticmethod
    def _bind_account(image: LibraryImage, current_digest: str) -> str:
        current_digest = _digest(current_digest)
        if image.account_digest is not None and image.account_digest != current_digest:
            raise PackageStale("account_changed")
        return current_digest

    async def async_list(self) -> dict[str, Any]:
        async with self._lock:
            self._guard()
            # Safe summaries remain visible after reauthentication so an admin can
            # explicitly delete the old account-bound records. Reconciliation and
            # every save/rebind still require the exact account digest.
            addresses = {item.alias_ref: item for item in self._image.addresses}
            return {
                "storeRevision": self._image.revision,
                "addresses": [item.public_dict() for item in self._image.addresses],
                "packages": [item.public_dict(addresses[item.address_ref]) for item in self._image.packages],
            }

    async def async_save_address(
        self,
        *,
        expected_store_revision: int,
        address_ref: str,
        expected_revision: int,
        name: str,
        match_digest: str,
        current_account_digest: str,
    ) -> AddressAlias:
        async with self._lock:
            self._write_guard(expected_store_revision)
            name = normalize_address_name(name)
            match_digest = _digest(match_digest)
            account = self._bind_account(self._image, current_account_digest)
            records = list(self._image.addresses)
            if any(
                normalize_lookup_key(item.name) == normalize_lookup_key(name)
                and item.alias_ref != address_ref
                for item in records
            ):
                raise PackageLibraryError
            if address_ref == "":
                if _strict_int(expected_revision, minimum=0) != 0 or len(records) >= MAX_ADDRESSES:
                    raise PackageLibraryError
                ref = self._new_ref("addr", {item.alias_ref for item in records})
                record = AddressAlias(ref, 1, name, match_digest)
                records.append(record)
            else:
                expected_revision = _strict_int(expected_revision, minimum=1)
                matches = [index for index, item in enumerate(records) if item.alias_ref == address_ref]
                if len(matches) != 1 or records[matches[0]].revision != expected_revision or expected_revision >= MAX_REVISION:
                    raise PackageLibraryError
                record = AddressAlias(address_ref, expected_revision + 1, name, match_digest)
                records[matches[0]] = record
            candidate = LibraryImage(self._image.revision + 1, account, tuple(records), self._image.packages)
            await self._async_save_candidate(candidate)
            return record

    async def async_delete_address(
        self, *, expected_store_revision: int, address_ref: str, expected_revision: int
    ) -> None:
        async with self._lock:
            self._write_guard(expected_store_revision)
            expected_revision = _strict_int(expected_revision, minimum=1)
            if any(item.address_ref == address_ref for item in self._image.packages):
                raise PackageLibraryError
            records = list(self._image.addresses)
            matches = [index for index, item in enumerate(records) if item.alias_ref == address_ref]
            if len(matches) != 1 or records[matches[0]].revision != expected_revision:
                raise PackageLibraryError
            records.pop(matches[0])
            account = self._image.account_digest if records or self._image.packages else None
            await self._async_save_candidate(
                LibraryImage(self._image.revision + 1, account, tuple(records), self._image.packages)
            )

    async def async_save_package(
        self,
        *,
        expected_store_revision: int,
        package_ref: str,
        expected_revision: int,
        name: str,
        aliases: Sequence[str],
        address_ref: str,
        address_revision: int,
        store: LiveStore,
        items: tuple[RecipeItem, ...],
        current_account_digest: str,
    ) -> PackageRecord:
        async with self._lock:
            self._write_guard(expected_store_revision)
            name = normalize_package_name(name)
            aliases = normalize_aliases(aliases, package_name=name)
            account = self._bind_account(self._image, current_account_digest)
            address_matches = [item for item in self._image.addresses if item.alias_ref == address_ref]
            address_revision = _strict_int(address_revision, minimum=1)
            if len(address_matches) != 1 or address_matches[0].revision != address_revision:
                raise PackageLibraryError
            records = list(self._image.packages)
            new_tokens = {
                normalize_lookup_key(name),
                *(normalize_lookup_key(item) for item in aliases),
            }
            for item in records:
                if item.package_ref != package_ref and new_tokens.intersection(
                    {
                        normalize_lookup_key(item.name),
                        *(normalize_lookup_key(alias) for alias in item.aliases),
                    }
                ):
                    raise PackageLibraryError
            if package_ref == "":
                if _strict_int(expected_revision, minimum=0) != 0 or len(records) >= MAX_PACKAGES:
                    raise PackageLibraryError
                ref = self._new_ref("pkg", {item.package_ref for item in records})
                revision = 1
                index = None
            else:
                expected_revision = _strict_int(expected_revision, minimum=1)
                matches = [index for index, item in enumerate(records) if item.package_ref == package_ref]
                if len(matches) != 1 or records[matches[0]].revision != expected_revision or expected_revision >= MAX_REVISION:
                    raise PackageLibraryError
                ref = package_ref
                revision = expected_revision + 1
                index = matches[0]
            record = PackageRecord(
                ref, revision, name, tuple(aliases), address_ref, address_revision,
                store.slug, store_digest(store), store.name, items,
            )
            if index is None:
                records.append(record)
            else:
                records[index] = record
            await self._async_save_candidate(
                LibraryImage(self._image.revision + 1, account, self._image.addresses, tuple(records))
            )
            return record

    async def async_delete_package(
        self, *, expected_store_revision: int, package_ref: str, expected_revision: int
    ) -> None:
        async with self._lock:
            self._write_guard(expected_store_revision)
            expected_revision = _strict_int(expected_revision, minimum=1)
            records = list(self._image.packages)
            matches = [index for index, item in enumerate(records) if item.package_ref == package_ref]
            if len(matches) != 1 or records[matches[0]].revision != expected_revision:
                raise PackageLibraryError
            records.pop(matches[0])
            account = self._image.account_digest if records or self._image.addresses else None
            await self._async_save_candidate(
                LibraryImage(self._image.revision + 1, account, self._image.addresses, tuple(records))
            )

    async def async_address_record(
        self,
        *,
        address_ref: str,
        address_revision: int,
        current_account_digest: str,
    ) -> AddressAlias:
        async with self._lock:
            self._guard()
            self._bind_account(self._image, current_account_digest)
            matches = [
                item
                for item in self._image.addresses
                if item.alias_ref == address_ref and item.revision == address_revision
            ]
            if len(matches) != 1:
                raise PackageLibraryError
            return matches[0]

    async def async_assert_account(self, current_account_digest: str) -> None:
        async with self._lock:
            self._guard()
            self._bind_account(self._image, current_account_digest)

    async def async_prepare_records(
        self, *, package_key: str, address_key: str
    ) -> tuple[PackageRecord, AddressAlias, int]:
        async with self._lock:
            self._guard()
            package_lookup = normalize_lookup_key(package_key)
            packages = [
                item
                for item in self._image.packages
                if package_lookup
                in {
                    item.package_ref.casefold(),
                    normalize_lookup_key(item.name),
                    *(normalize_lookup_key(alias) for alias in item.aliases),
                }
            ]
            if len(packages) != 1:
                raise PackageLibraryError
            package = packages[0]
            if address_key == "":
                addresses = [item for item in self._image.addresses if item.alias_ref == package.address_ref]
            else:
                address_lookup = normalize_lookup_key(address_key)
                addresses = [
                    item for item in self._image.addresses
                    if address_lookup
                    in {item.alias_ref.casefold(), normalize_lookup_key(item.name)}
                ]
            if len(addresses) != 1:
                raise PackageLibraryError
            return package, addresses[0], self._image.revision

    async def async_assert_current(
        self,
        *,
        store_revision: int,
        package_ref: str,
        package_revision: int,
        address_ref: str,
        address_revision: int,
    ) -> None:
        async with self._lock:
            self._guard()
            if self._image.revision != store_revision:
                raise PackageLibraryError
            if not any(item.package_ref == package_ref and item.revision == package_revision for item in self._image.packages):
                raise PackageLibraryError
            if not any(item.alias_ref == address_ref and item.revision == address_revision for item in self._image.addresses):
                raise PackageLibraryError


def recipe_items_from_capture(
    captured: Sequence[tuple[CatalogProduct, int, Sequence[tuple[CatalogOptionGroup, Sequence[CatalogOption]]]]]
) -> tuple[RecipeItem, ...]:
    """Convert registry-resolved selections to immutable private recipes."""
    result: list[RecipeItem] = []
    for product, quantity, groups in captured:
        recipe_groups = tuple(
            RecipeGroup(
                group_digest(group),
                group.label,
                tuple(option_digest(group, option) for option in options),
                tuple(option.label for option in options),
            )
            for group, options in groups
            if options
        )
        result.append(RecipeItem(product_digest(product), product.name, quantity, recipe_groups))
    return tuple(result)


def reconcile_recipe(
    package: PackageRecord,
    store: LiveStore,
    products: Sequence[CatalogProduct],
) -> tuple[tuple[CatalogProduct, int, tuple[tuple[CatalogOptionGroup, tuple[CatalogOption, ...]], ...]], ...]:
    """Require exact unique current identity matches and current constraints."""
    if store_digest(store) != package.store_digest:
        raise PackageStale("store_missing_or_changed")
    reconciled: list[tuple[CatalogProduct, int, tuple[tuple[CatalogOptionGroup, tuple[CatalogOption, ...]], ...]]] = []
    for item in package.items:
        matches = [product for product in products if product_digest(product) == item.product_digest]
        if len(matches) != 1:
            raise PackageStale("product_missing_or_changed")
        product = matches[0]
        recipe_groups = {group.group_digest: group for group in item.option_groups}
        current_digests = [group_digest(group) for group in product.option_groups]
        if any(key not in current_digests for key in recipe_groups):
            raise PackageStale("option_missing_or_changed")
        groups: list[tuple[CatalogOptionGroup, tuple[CatalogOption, ...]]] = []
        for group in product.option_groups:
            recipe = recipe_groups.get(group_digest(group))
            if recipe is None:
                chosen: tuple[CatalogOption, ...] = ()
                if group.minimum > 0:
                    raise PackageStale("selection_constraints_changed")
            else:
                chosen_values: list[CatalogOption] = []
                for digest in recipe.option_digests:
                    options = [option for option in group.options if option_digest(group, option) == digest]
                    if len(options) != 1:
                        raise PackageStale("option_missing_or_changed")
                    chosen_values.append(options[0])
                chosen = tuple(chosen_values)
            if (
                not group.minimum <= len(chosen) <= group.maximum
                or (not group.multiple_selection and len(chosen) > 1)
            ):
                raise PackageStale("selection_constraints_changed")
            groups.append((group, chosen))
        reconciled.append((product, item.quantity, tuple(groups)))
    return tuple(reconciled)
