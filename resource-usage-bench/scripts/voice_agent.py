"""Free voice agent for the bench: a standard LiveKit Agents worker (production mode, its default warm
processes), Silero VAD, LiveKit's multilingual turn detector, faster-whisper STT and Piper TTS through
speech_server.py, and the fake LLM in fake_services.py (a hosted model is outside the server anyway).

  python voice_agent.py download-files   # turn-detector + Silero models into HF_HOME
  python voice_agent.py start            # production mode

Env: LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET, SPEECH_URL (http://speech:8100/v1),
LLM_URL (http://fakes:9000/v1), TURN_DETECTOR=1|0.
"""

from __future__ import annotations

import os

from livekit.agents import Agent, AgentSession, JobContext, JobProcess, WorkerOptions, cli
from livekit.plugins import openai, silero

USE_TURN_DETECTOR = os.environ.get("TURN_DETECTOR", "1") == "1"
if USE_TURN_DETECTOR:  # imported at load time so `download-files` fetches its model too
    from livekit.plugins.turn_detector.multilingual import MultilingualModel

SPEECH = os.environ.get("SPEECH_URL", "http://speech:8100/v1")
LLM = os.environ.get("LLM_URL", "http://fakes:9000/v1")


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    turn: object = MultilingualModel() if USE_TURN_DETECTOR else "vad"
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=openai.STT(base_url=SPEECH, api_key="bench", model="whisper-turbo", detect_language=True,
                       use_realtime=False),
        llm=openai.LLM(base_url=LLM, api_key="bench", model="bench-llm"),
        tts=openai.TTS(base_url=SPEECH, api_key="bench", model="piper", voice="en", response_format="pcm"),
        turn_detection=turn,
    )
    await session.start(room=ctx.room, agent=Agent(instructions="You are Arifa Pack's sales assistant."))


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
