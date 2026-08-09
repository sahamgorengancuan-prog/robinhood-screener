"""Robinhood Chain Node API client (standard Ethereum JSON-RPC).

This client depends only on things that are *standardised*: JSON-RPC method
names, the ERC-20 ABI, the ERC-20 `Transfer` event topic, and the EIP-1967 proxy
storage slot. That makes it the trustworthy floor of the whole system — if the
indexed Data API is unavailable or its response shape differs from what we
expect, holder distribution, supply and contract flags can still be rebuilt
here from raw logs.

Cost note: this is free against any RPC provider, but `eth_getLogs` over a wide
block range is the expensive call. `LOG_CHUNK_BLOCKS` keeps each request small
and the caller decides how far back to walk.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError

log = logging.getLogger(__name__)

# --- standard constants (not guesses) ---------------------------------------
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# EIP-1967: bytes32(uint256(keccak256('eip1967.proxy.implementation')) - 1)
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

SELECTORS = {
    "totalSupply()": "0x18160ddd",
    "decimals()": "0x313ce567",
    "symbol()": "0x95d89b41",
    "name()": "0x06fdde03",
    "owner()": "0x8da5cb5b",
    "balanceOf(address)": "0x70a08231",
}

# Bytecode fingerprints for privileged / hostile functions. Presence of the
# 4-byte selector in runtime code is evidence the function exists; absence is
# reasonably strong evidence it does not (barring proxies — hence is_proxy).
DANGEROUS_SELECTORS = {
    "40c10f19": "mint(address,uint256)",
    "42966c68": "burn(uint256)",
    "8456cb59": "pause()",
    "5c975abb": "paused()",
    "f2fde38b": "transferOwnership(address)",
    "715018a6": "renounceOwnership()",
    "0ecb93c0": "addBlackList(address)",
    "e4997dc5": "removeBlackList(address)",
    "44337ea1": "blacklist(address)",
    "3f4ba83a": "unpause()",
    "01e33667": "setFees(uint256,uint256)",
    "c0246668": "excludeFromFees(address,bool)",
    "751039fc": "removeLimits()",
    "a457c2d7": "decreaseAllowance(address,uint256)",
}
# Only these actually justify a risk flag; the rest are informational.
FLAGGED_SELECTORS = {
    "40c10f19": "MINT_FUNCTION",
    "8456cb59": "PAUSABLE",
    "0ecb93c0": "BLACKLIST",
    "44337ea1": "BLACKLIST",
    "01e33667": "MUTABLE_FEES",
    "751039fc": "MUTABLE_LIMITS",
}

LOG_CHUNK_BLOCKS = 2000


def _hex_to_int(h: Any) -> int | None:
    if not isinstance(h, str) or not h.startswith("0x"):
        return None
    try:
        return int(h, 16)
    except ValueError:
        return None


def _decode_string(hexdata: str) -> str | None:
    """Decode an ABI-encoded `string` return, falling back to bytes32."""
    if not hexdata or hexdata == "0x":
        return None
    raw = bytes.fromhex(hexdata[2:])
    try:
        if len(raw) >= 64:
            length = int.from_bytes(raw[32:64], "big")
            if 0 < length <= len(raw) - 64:
                return raw[64 : 64 + length].decode("utf-8", errors="replace").strip("\x00") or None
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip("\x00") or None
    except Exception:
        return None


def topic_to_address(topic: str) -> str:
    return "0x" + topic[-40:]


class RobinhoodNodeClient(BaseHTTPClient):
    name = "rh_node"

    def __init__(self, rpc_url: str, **kw: Any) -> None:
        super().__init__(rpc_url, **kw)
        self._id = 0
        self.enabled = bool(rpc_url)

    async def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        if not self.enabled:
            raise ClientError("rh_node: RH_NODE_RPC_URL not configured")
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        data = await self.request("POST", "", json_body=payload)
        if isinstance(data, dict) and data.get("error"):
            raise ClientError(f"rh_node {method}: {data['error']}")
        return (data or {}).get("result")

    # ------------------------------------------------------------- chain info
    async def chain_id(self) -> int | None:
        return _hex_to_int(await self.rpc("eth_chainId"))

    async def block_number(self) -> int | None:
        return _hex_to_int(await self.rpc("eth_blockNumber"))

    async def get_block_timestamp(self, block: int | str = "latest") -> int | None:
        tag = block if isinstance(block, str) else hex(block)
        blk = await self.rpc("eth_getBlockByNumber", [tag, False])
        return _hex_to_int((blk or {}).get("timestamp"))

    # ----------------------------------------------------------------- erc20
    async def call(self, to: str, data: str) -> str | None:
        return await self.rpc("eth_call", [{"to": to, "data": data}, "latest"])

    async def erc20_total_supply(self, address: str) -> int | None:
        return _hex_to_int(await self.call(address, SELECTORS["totalSupply()"]))

    async def erc20_decimals(self, address: str) -> int | None:
        v = _hex_to_int(await self.call(address, SELECTORS["decimals()"]))
        return v if v is not None and 0 <= v <= 36 else None

    async def erc20_symbol(self, address: str) -> str | None:
        try:
            return _decode_string(await self.call(address, SELECTORS["symbol()"]) or "")
        except ClientError:
            return None

    async def erc20_name(self, address: str) -> str | None:
        try:
            return _decode_string(await self.call(address, SELECTORS["name()"]) or "")
        except ClientError:
            return None

    async def erc20_balance_of(self, token: str, holder: str) -> int | None:
        data = SELECTORS["balanceOf(address)"] + holder.lower().replace("0x", "").rjust(64, "0")
        return _hex_to_int(await self.call(token, data))

    async def owner(self, address: str) -> str | None:
        try:
            res = await self.call(address, SELECTORS["owner()"])
        except ClientError:
            return None
        if not res or res == "0x":
            return None
        addr = "0x" + res[-40:]
        return None if addr == ZERO_ADDRESS else addr

    # -------------------------------------------------------------- contract
    async def get_code(self, address: str) -> str | None:
        code = await self.rpc("eth_getCode", [address, "latest"])
        return code if code and code != "0x" else None

    async def is_proxy(self, address: str) -> bool | None:
        """EIP-1967 implementation slot non-zero => upgradeable proxy."""
        try:
            slot = await self.rpc("eth_getStorageAt", [address, EIP1967_IMPL_SLOT, "latest"])
        except ClientError:
            return None
        if not isinstance(slot, str):
            return None
        return int(slot, 16) != 0 if slot.startswith("0x") else None

    async def contract_risk_flags(self, address: str) -> tuple[list[str], dict[str, Any]]:
        """Static bytecode scan. Returns (flags, detail)."""
        flags: list[str] = []
        detail: dict[str, Any] = {}

        code = await self.get_code(address)
        if not code:
            return ["NOT_A_CONTRACT"], {"code": None}

        body = code.lower()
        found = [name for sel, name in DANGEROUS_SELECTORS.items() if sel in body]
        detail["functions_detected"] = found
        detail["code_size_bytes"] = (len(code) - 2) // 2

        for sel, flag in FLAGGED_SELECTORS.items():
            if sel in body and flag not in flags:
                flags.append(flag)

        proxy = await self.is_proxy(address)
        detail["is_proxy"] = proxy
        if proxy:
            flags.append("UPGRADEABLE_PROXY")

        own = await self.owner(address)
        detail["owner"] = own
        if own:
            flags.append("OWNER_PRIVILEGE_ACTIVE")
            # Mint + live owner is the classic infinite-supply rug setup.
            if "MINT_FUNCTION" in flags:
                flags.append("OWNER_CAN_MINT")

        if detail["code_size_bytes"] and detail["code_size_bytes"] < 500:
            flags.append("SUSPICIOUSLY_SMALL_BYTECODE")

        return flags, detail

    # ------------------------------------------------------------------ logs
    async def get_transfer_logs(self, address: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
        """Fetch Transfer logs in bounded chunks."""
        out: list[dict[str, Any]] = []
        start = from_block
        while start <= to_block:
            end = min(start + LOG_CHUNK_BLOCKS - 1, to_block)
            res = await self.rpc(
                "eth_getLogs",
                [{"address": address, "fromBlock": hex(start), "toBlock": hex(end), "topics": [TRANSFER_TOPIC]}],
            )
            if isinstance(res, list):
                out.extend(res)
            start = end + 1
        return out

    @staticmethod
    def holders_from_logs(logs: list[dict[str, Any]]) -> dict[str, int]:
        """Reconstruct balances from Transfer logs.

        Only correct if `logs` covers the token's entire history from deployment.
        Callers that start mid-history must treat the output as a *lower bound*
        on distribution quality and mark the metric unavailable rather than
        reporting a wrong number.
        """
        balances: dict[str, int] = {}
        for lg in logs:
            topics = lg.get("topics") or []
            if len(topics) < 3:
                continue
            frm = topic_to_address(topics[1]).lower()
            to = topic_to_address(topics[2]).lower()
            val = _hex_to_int(lg.get("data")) or 0
            if frm != ZERO_ADDRESS:
                balances[frm] = balances.get(frm, 0) - val
            if to != ZERO_ADDRESS:
                balances[to] = balances.get(to, 0) + val
        return {k: v for k, v in balances.items() if v > 0}

    async def find_deploy_block(self, address: str, latest: int) -> int | None:
        """Binary search for the first block where the address has code.

        ~log2(latest) eth_getCode calls (about 25 for a 30M-block chain). Cheap
        enough to be worth it, and it gives a real token_age instead of an
        assumed one.
        """
        try:
            if not await self.get_code(address):
                return None
        except ClientError:
            return None

        lo, hi = 0, latest
        while lo < hi:
            mid = (lo + hi) // 2
            try:
                code = await self.rpc("eth_getCode", [address, hex(mid)])
            except ClientError:
                return None
            if code and code != "0x":
                hi = mid
            else:
                lo = mid + 1
        return lo
