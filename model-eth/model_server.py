from fastapi import FastAPI
from pydantic import BaseModel
import joblib
import numpy as np

app = FastAPI()

# Cargar modelo entrenado (scikit-learn, por ejemplo)
MODEL_PATH = "model/latest_model.pkl"
model = joblib.load(MODEL_PATH)


class FeatureVector(BaseModel):
    features: list[float]


class Prediction(BaseModel):
    action: str
    confidence: float


@app.post("/predict", response_model=Prediction)
def predict(features: FeatureVector):
    X = np.array(features.features).reshape(1, -1)
    proba = model.predict_proba(X)[0]
    # asumimos clases [sell, hold, buy] solo como ejemplo
    classes = model.classes_
    max_idx = proba.argmax()
    label = classes[max_idx]

    # mapear label a acción
    if label == 1:
        action = "buy"
    elif label == -1:
        action = "sell"
    else:
        action = "hold"

    return Prediction(action=action, confidence=float(proba[max_idx]))
