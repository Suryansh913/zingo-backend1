"""Automated REAL WebRTC media test (not a browser): two aiortc peers negotiate SDP/ICE/DTLS through the real MatchConsumer
signaling server and must actually receive video and audio frames from each other. Skipped if aiortc isn't installed."""
import asyncio, unittest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from .test_match import DEV, ORIGIN, app, mk, pair, tok
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack
except ImportError: RTCPeerConnection = None

@unittest.skipIf(RTCPeerConnection is None, "aiortc not installed")
@DEV
class RealWebRtcMediaTest(TransactionTestCase):
    async def open_peer(self, user, token, sid):
        c = WebsocketCommunicator(app(), "/ws/match/", headers=ORIGIN); assert (await c.connect())[0]
        await c.send_json_to({"type": "auth", "token": token}); assert (await c.receive_json_from())["type"] == "ready"; await c.receive_json_from()
        await c.send_json_to({"type": "join", "session": sid}); assert (await c.receive_json_from())["type"] == "joined"
        return c

    async def run_peer(self, comm, sid, caller, got):
        pc = RTCPeerConnection(); pc.addTrack(VideoStreamTrack()); pc.addTrack(AudioStreamTrack())
        async def consume(track):
            await track.recv(); got[track.kind].set()                      # an actual decoded frame crossed the network path
        @pc.on("track")
        def on_track(track): asyncio.ensure_future(consume(track))
        send = lambda kind, data: comm.send_json_to({"type": "signal", "session": sid, "kind": kind, "data": data})
        async def offer():
            await pc.setLocalDescription(await pc.createOffer()); await send("offer", {"sdp": pc.localDescription.sdp, "type": "offer"})
        async def pump():
            while True:
                m = await comm.receive_json_from(timeout=60)
                if m.get("type") != "signal": continue
                k, d = m["kind"], m["data"]
                if k == "ready" and caller: await offer()
                elif k == "offer":
                    await pc.setRemoteDescription(RTCSessionDescription(d["sdp"], d["type"]))
                    await pc.setLocalDescription(await pc.createAnswer()); await send("answer", {"sdp": pc.localDescription.sdp, "type": "answer"})
                elif k == "answer": await pc.setRemoteDescription(RTCSessionDescription(d["sdp"], d["type"]))
        return pc, asyncio.ensure_future(pump()), (lambda: send("ready", {}))

    async def test_two_peers_exchange_real_audio_and_video_via_signaling(self):
        a, b = await sync_to_async(mk)("alice"), await sync_to_async(mk)("bob"); s = await sync_to_async(pair)(a, b)
        ta, tb = await sync_to_async(tok)(a), await sync_to_async(tok)(b)
        ca, cb = await self.open_peer(a, ta, s.id), await self.open_peer(b, tb, s.id)
        ga = {k: asyncio.Event() for k in ("video", "audio")}; gb = {k: asyncio.Event() for k in ("video", "audio")}
        pa, ta_, ready_a = await self.run_peer(ca, s.id, True, ga); pb, tb_, ready_b = await self.run_peer(cb, s.id, False, gb)
        try:
            await ready_a(); await ready_b()
            await asyncio.wait_for(asyncio.gather(*[e.wait() for e in (*ga.values(), *gb.values())]), timeout=40)
            self.assertEqual((pa.connectionState, pb.connectionState), ("connected", "connected"))
        finally:
            for t in (ta_, tb_): t.cancel()
            await pa.close(); await pb.close(); await ca.disconnect(); await cb.disconnect()
