from __future__ import annotations

from typing import List, Tuple
from common.logger import BotLogger
import requests


class ModelClient:
    """
    Cliente HTTP simple para el microservicio de modelo.

    Se puede reutilizar tal cual en otros bots (scalping, etc.)
    y es fácilmente mockeable en tests.
    """

    def __init__(self, model_url: str) -> None:
        self.log = BotLogger("foundational_bot")
        self.model_url = model_url

    def predict(self, features: List[float]) -> Tuple[str, float]:
        """
        Envía el vector de features y devuelve (action, confidence).

        action: "buy" | "sell" | "hold"
        confidence: probabilidad asociada a la acción elegida.
        """
        payload = {"features": features}
        try:
            resp = requests.post(self.model_url, json=payload, timeout=2)
            resp.raise_for_status()
        except requests.RequestException as exc:
            # MVP: no rompemos el loop; devolvemos 'hold'
            self.log.error(f"[MODEL] Error querying the model: {exc}")
            return "hold", 0.0

        data = resp.json()
        action = data.get("action", "hold")
        confidence = float(data.get("confidence", 0.0))
        return action, confidence