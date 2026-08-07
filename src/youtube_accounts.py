"""Several YouTube channels behind one dashboard channel.

A dashboard channel (kids, crime, trending …) decides the topics, the voice and
the look. Each YouTube channel it publishes to is an *account* here. Every
account owns its own OAuth token, and — if you want its own upload quota — its
own Google Cloud client secret.

Topics are never shared between accounts of the same channel: the daily run
hands each account its own untouched topics, so the same video never appears on
two of your YouTube channels and nothing is flagged as reused content.

Accounts live in data/youtube_accounts.json:

    {
      "kids": [
        {"id": "main",  "name": "Kids Learning"},
        {"id": "abc",   "name": "Kids ABC World", "client_secret": "client_secret_kids_abc.json"}
      ]
    }

A channel with no entry has exactly one account, "main", using the token and
client secret already written in channels.json — so an untouched setup keeps
behaving exactly as before.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .channels import Channel, get_channel, load_channels
from .config import settings

ACCOUNTS_FILE = settings.root / "data" / "youtube_accounts.json"
MAIN_ACCOUNT = "main"

_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Account:
    channel_id: str
    id: str
    name: str
    token: str
    client_secret: str

    @property
    def token_path(self) -> Path:
        return settings.root / self.token

    @property
    def client_secret_path(self) -> Path:
        return settings.root / self.client_secret

    @property
    def connected(self) -> bool:
        return self.token_path.is_file()

    @property
    def label(self) -> str:
        return self.name or self.id


def _read_registry() -> dict[str, list[dict]]:
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_registry(registry: dict[str, list[dict]]) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def default_token_name(channel_id: str, account_id: str) -> str:
    """Token filename for one account, keeping the original name for "main"."""
    if account_id == MAIN_ACCOUNT:
        return get_channel(channel_id).token
    return f"token_{channel_id}_{account_id}.json"


def _account_from_entry(channel: Channel, entry: dict) -> Account:
    account_id = str(entry.get("id") or MAIN_ACCOUNT)
    return Account(
        channel_id=channel.id,
        id=account_id,
        name=str(entry.get("name") or channel.name),
        token=str(entry.get("token") or default_token_name(channel.id, account_id)),
        # Sharing the channel's client secret means sharing its daily upload
        # quota; give an account its own secret to give it its own quota.
        client_secret=str(entry.get("client_secret") or channel.client_secret),
    )


def _main_account(channel: Channel) -> Account:
    return Account(
        channel_id=channel.id, id=MAIN_ACCOUNT, name=channel.name,
        token=channel.token, client_secret=channel.client_secret,
    )


def list_accounts(channel_id: str) -> list[Account]:
    """Every YouTube account of one channel; always at least the main one."""
    channel = get_channel(channel_id)
    entries = _read_registry().get(channel_id) or []
    accounts = [_account_from_entry(channel, entry) for entry in entries if entry.get("id")]
    if not any(account.id == MAIN_ACCOUNT for account in accounts):
        accounts.insert(0, _main_account(channel))
    return accounts


def get_account(channel_id: str, account_id: str | None) -> Account:
    wanted = str(account_id or MAIN_ACCOUNT)
    for account in list_accounts(channel_id):
        if account.id == wanted:
            return account
    raise KeyError(f"Channel '{channel_id}' has no account '{wanted}'")


def connected_accounts(channel_id: str) -> list[Account]:
    """Accounts that finished the one-time YouTube authorization."""
    return [account for account in list_accounts(channel_id) if account.connected]


def channel_for_account(channel_id: str, account_id: str | None) -> Channel:
    """The channel as seen by one account: same content, that account's login.

    Everything downstream (rendering, SEO, upload, analytics) already takes a
    Channel, so pointing its token and client secret at one account is all that
    is needed to publish to a different YouTube channel.
    """
    channel = get_channel(channel_id)
    account = get_account(channel_id, account_id)
    return dataclasses.replace(
        channel, account=account.id, name=account.name,
        token=account.token, client_secret=account.client_secret,
    )


def add_account(channel_id: str, account_id: str, name: str = "",
                client_secret: str = "") -> Account:
    """Register another YouTube channel under this dashboard channel."""
    account_id = str(account_id or "").strip().lower()
    if not _VALID_ID.match(account_id):
        raise ValueError(
            "Account id must be lowercase letters, digits, '-' or '_' (max 32 characters)."
        )
    get_channel(channel_id)  # raises for an unknown channel
    registry = _read_registry()
    entries = list(registry.get(channel_id) or [])
    if not any(str(entry.get("id")) == MAIN_ACCOUNT for entry in entries):
        entries.insert(0, {"id": MAIN_ACCOUNT})
    if any(str(entry.get("id")) == account_id for entry in entries):
        raise ValueError(f"Channel '{channel_id}' already has an account '{account_id}'.")
    entry: dict[str, str] = {
        "id": account_id,
        "name": name.strip() or f"{get_channel(channel_id).name} {account_id}",
        "token": default_token_name(channel_id, account_id),
    }
    if client_secret.strip():
        entry["client_secret"] = client_secret.strip()
    entries.append(entry)
    registry[channel_id] = entries
    _write_registry(registry)
    return get_account(channel_id, account_id)


def next_account_id(channel_id: str) -> str:
    """A free id like ch2, ch3 … so nobody has to invent one."""
    taken = {account.id for account in list_accounts(channel_id)}
    index = 2
    while f"ch{index}" in taken:
        index += 1
    return f"ch{index}"


def set_account_name(channel_id: str, account_id: str, name: str) -> None:
    """Rename an account, normally to the real YouTube channel title.

    The dashboard calls this straight after a successful sign-in, so the row
    shows the actual channel name without anyone typing it.
    """
    name = name.strip()
    if not name or account_id == MAIN_ACCOUNT:
        return
    registry = _read_registry()
    entries = list(registry.get(channel_id) or [])
    for entry in entries:
        if str(entry.get("id")) == str(account_id):
            entry["name"] = name
            registry[channel_id] = entries
            _write_registry(registry)
            return


def remove_account(channel_id: str, account_id: str) -> None:
    """Forget an account. The main account cannot be removed."""
    if str(account_id) == MAIN_ACCOUNT:
        raise ValueError("The main account cannot be removed.")
    registry = _read_registry()
    entries = [
        entry for entry in (registry.get(channel_id) or [])
        if str(entry.get("id")) != str(account_id)
    ]
    registry[channel_id] = entries
    _write_registry(registry)


def all_connected_accounts() -> list[Account]:
    return [
        account
        for channel in load_channels()
        for account in connected_accounts(channel.id)
    ]
