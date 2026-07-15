"""Mock telephony adapter for local development.

The mock client is the browser-mic page in `services/mock_telephony/`. It speaks
the **same** WebSocket protocol as the Tata adapter — same `start` / `media` /
`stop` envelope — so the bridge code path is identical between dev and prod.
That's the whole point: the only difference between calling from a browser and
calling from a real phone via Tata is which client opens the WebSocket.
"""

from __future__ import annotations

from app.telephony.tata import TataAdapter


class MockAdapter(TataAdapter):
    """Same wire protocol as Tata; documented separately for clarity.

    Subclassing keeps the audio loop identical. If your local mock evolves to
    differ from Tata's protocol, override only the methods you need to.
    """

    pass
