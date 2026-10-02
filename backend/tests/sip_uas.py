"""A minimal SIP answerer that records the RTP it is sent.

Exists because the real symptom — "the phone rings, nobody hears APEX" — cannot
be reproduced against a handset without involving a person, and because the
question it answers is narrow and mechanical: does audio played into the
transmit sink arrive, as RTP, at the far end? Nothing here speaks SIP properly
beyond the single INVITE/200/ACK exchange that a UDP call needs, and nothing
here is used in production.
"""
from __future__ import annotations

import re
import socket
import threading


def _sdp_body(text: str) -> str:
    start = text.find("\r\n\r\n")
    return text[start + 4:] if start >= 0 else ""


def _rtp_port(offer: str) -> int:
    match = re.search(r"m=audio\s+(\d+)", offer)
    return int(match.group(1)) if match else 0


def _answer_body(sdp_offer: str, rtp_port: int, call_id: str, local: str) -> str:
    """A 200 OK echoing the request's headers and carrying our own SDP.

    The headers are copied verbatim out of the INVITE. Content-Length has to be
    the real SDP length: a zero length followed by an SDP body leaves the peer's
    parser reading past the end of the message and rejecting the call, which
    looks exactly like an unanswered call from this side.
    """
    def header(name: str) -> str:
        found = re.search(rf"^{name}:(.*)$", sdp_offer, re.M)
        return found.group(1).strip() if found else ""

    sdp = (
        f"v=0\r\n"
        f"o=- 1 1 IN IP4 {local.rsplit(':', 1)[0]}\r\n"
        f"s=-\r\n"
        f"c=IN IP4 {local.rsplit(':', 1)[0]}\r\n"
        f"t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0 8\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=sendrecv\r\n"
    )
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {header('Via')}\r\n"
        f"From: {header('From')}\r\n"
        f"To: {header('To')};tag=local-tag\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {header('CSeq')}\r\n"
        f"Contact: <sip:{local}>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n"
        f"\r\n"
        f"{sdp}"
    )


class Answerer:
    """Answers one INVITE and records every RTP byte received."""

    def __init__(self, host: str = "127.0.0.1", sip_port: int = 5080):
        self.host = host
        self.sip_port = sip_port
        self.rtp_port = rtp_port = 41000 + (sip_port % 1000)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, sip_port))
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind((host, self.rtp_port))
        self.rtp.settimeout(0.2)
        self.payloads: list[bytes] = []
        self.raw_count = 0
        self.raw_sample: bytes = b''
        self.answered = threading.Event()
        self.hangup = threading.Event()
        self._dialog: dict | None = None
        self._stop = threading.Event()
        self._target = None
        self._threads = []
        self._port = rtp_port

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "Answerer":
        for target, name in ((self._sip_loop, "sip"), (self._rtp_loop, "rtp")):
            thread = threading.Thread(target=target, daemon=True, name=f"uas-{name}")
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stop.set()
        for sock in (self.sock, self.rtp):
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- loops -----------------------------------------------------------
    def _rtp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self.rtp.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            if not data:
                continue
            self.raw_count += 1
            if not self.raw_sample:
                self.raw_sample = data[:24]
            # Payloads are kept per packet. Re-parsing a concatenated byte
            # stream would have to guess packet boundaries, and UDP gives no
            # such guarantee - recvfrom can hand back a partial packet or two
            # packets in one read.
            if len(data) > 12:
                # Fixed 12-byte header, plus 4 bytes per CSRC, plus the
                # extension block when the X bit is set. PCMU/PCMA packets
                # from baresip carry none of either, so this is normally 12.
                csrc = data[0] & 0x0F
                header_len = 12 + 4 * csrc
                if data[0] & 0x10 and len(data) >= header_len + 4:
                    words = int.from_bytes(
                        data[header_len + 2:header_len + 4], "big")
                    header_len += 4 + 4 * words
                payload_len = len(data) - header_len
                if 0 < payload_len <= 4096:
                    self.payloads.append(data[header_len:])
            if self._target:
                try:
                    self._target.sendto(data, self._target)
                except OSError:
                    pass

    def _sip_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            text = data.decode("utf-8", "replace")
            if text.startswith("SIP/2.0"):
                if "BYE" in text.split("\r\n")[0]:
                    self.hangup.set()
                continue
            if "INVITE" not in text.split("\r\n")[0]:
                continue
            call_id = re.search(r"^Call-ID:(.*)$", text, re.M)
            offer = _sdp_body(text)
            reply = _answer_body(text, self._port,
                                 call_id.group(1).strip() if call_id else "unknown",
                                 f"{self.host}:{self.sip_port}")
            # The ACK goes to the Contact we advertised, so send it there.
            self.sock.sendto(reply.encode(), addr)
            self._dialog = {"text": text, "addr": addr, "reply": reply}
            self.answered.set()

    def send_bye(self) -> bool:
        """Hang up the far end, the way a real caller does.

        Sent to the source of the INVITE, with the roles of From/To swapped
        relative to the INVITE and our tag added, which is what makes it a
        request inside the established dialog rather than an in-dialog message
        baresip would drop.
        """
        d = self._dialog
        if not d:
            return False
        text = d["text"]

        def header(name: str) -> str:
            found = re.search(rf"^{name}:(.*)$", text, re.M)
            return found.group(1).strip() if found else ""

        cseq = header("CSeq").split()[0] if header("CSeq") else "1"
        try:
            cseq = str(int(cseq) + 1)
        except ValueError:
            cseq = "2"
        bye = (
            f"BYE sip:{self.host}:{self.sip_port} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.host}:{self.sip_port};branch=z9hG4bK-bye\r\n"
            f"From: {header('To')};tag=local-tag\r\n"
            f"To: {header('From')}\r\n"
            f"Call-ID: {header('Call-ID')}\r\n"
            f"CSeq: {cseq} BYE\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self.sock.sendto(bye.encode(), d["addr"])
        return True

    # -- assertions ------------------------------------------------------
    def wait_answered(self, timeout: float = 30.0) -> bool:
        return self.answered.wait(timeout)

    def audio_bytes(self) -> int:
        """Total RTP payload received, in bytes."""
        return sum(len(p) for p in self.payloads)

    def non_silent_bytes(self) -> int:
        """Payload bytes that are not G.711 silence.

        G.711 mu-law silence is 0xFF and A-law silence is 0xD5, but a decoder
        receiving pure digital silence is not the question here: the question
        is whether anything arrived that a listener would hear. Counting the
        near-silence values keeps a stream of padding from reading as voice.
        """
        return sum(1 for payload in self.payloads for b in payload
                   if b not in (0x00, 0xFF, 0xD5, 0x55))