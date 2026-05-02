import asyncio
import datetime

SEP = "\x01"
TARGET = "EXCHANGE"


def _checksum(data: str) -> str:
    return f"{sum(data.encode('ascii')) % 256:03d}"


def build_message(msg_type: str, seq: int, body_fields: dict, sender: str) -> bytes:
    ts = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d-%H:%M:%S")
    header = (
        f"35={msg_type}{SEP}"
        f"49={sender}{SEP}"
        f"56={TARGET}{SEP}"
        f"34={seq}{SEP}"
        f"52={ts}{SEP}"
    )
    body = header + "".join(f"{k}={v}{SEP}" for k, v in body_fields.items())
    prefix = f"8=FIX.4.2{SEP}9={len(body.encode('ascii'))}{SEP}"
    raw = prefix + body
    raw += f"10={_checksum(raw)}{SEP}"
    return raw.encode("ascii")


def parse_fields(raw: bytes) -> dict:
    fields = {}
    for pair in raw.decode("ascii", errors="replace").split("\x01"):
        if "=" in pair:
            tag, _, val = pair.partition("=")
            fields[tag] = val
    return fields


class AsyncFixSession:
    def __init__(self, sender: str):
        self.sender = sender
        self.seq = 1
        self._buf = b""
        self._reader = None
        self._writer = None
        self.order_statuses = []

    async def connect(self, host: str, port: int):
        self._reader, self._writer = await asyncio.open_connection(host, port)

    async def send(self, msg_type: str, body: dict):
        msg = build_message(msg_type, self.seq, body, self.sender)
        self.seq += 1
        self._writer.write(msg)
        await self._writer.drain()

    async def recv(self) -> dict:
        while True:
            if b"10=" in self._buf:
                end = self._buf.index(b"10=")
                soh = self._buf.index(b"\x01", end)
                msg = self._buf[: soh + 1]
                self._buf = self._buf[soh + 1:]
                return parse_fields(msg)
            chunk = await self._reader.read(4096)
            if not chunk:
                raise ConnectionError("Exchange closed connection")
            self._buf += chunk

    async def logon(self):
        self.order_statuses = []
        await self.send("A", {"98": "0", "108": "30"})
        resp = await self.recv()
        if resp.get("35") != "A":
            raise RuntimeError(f"Expected Logon response, got: {resp}")
        while True:
            try:
                msg = await asyncio.wait_for(self.recv(), timeout=0.3)
                if msg.get("35") == "8" and msg.get("150") == "I":
                    self.order_statuses.append(msg)
            except asyncio.TimeoutError:
                break
        return resp

    async def logout(self):
        try:
            await self.send("5", {"58": "Normal logout"})
        except Exception:
            pass

    async def close(self):
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass
