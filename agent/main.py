# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Whapi, Meta, Twilio) gracias a la capa de providers.
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import (inicializar_db, guardar_mensaje, obtener_historial,
                          pausar_conversacion, reanudar_conversacion, esta_pausada)
from agent.providers import obtener_proveedor

load_dotenv()

# Configuración de logging según entorno
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="AgentKit — Printealito (Printealo)",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "service": "agentkit", "agente": "Printealito"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (requerido por Meta Cloud API, no-op para otros)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


# Operadores autorizados para tomar control de conversaciones
COMANDOS_PAUSA = {"#abdiel", "#grace"}
COMANDO_REANUDAR = "#bot"


def detectar_comando_control(texto: str) -> str | None:
    """
    Detecta si el texto contiene un comando de control humano.
    Retorna el comando limpio o None si no hay ninguno.
    """
    texto_limpio = texto.strip().lower()
    if texto_limpio in COMANDOS_PAUSA:
        return texto_limpio
    if texto_limpio == COMANDO_REANUDAR:
        return COMANDO_REANUDAR
    return None


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Procesa el mensaje, genera respuesta con Claude y la envía de vuelta.
    Soporta control humano con #abdiel, #grace y #bot.
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if not msg.texto:
                continue

            # ── Comandos de control humano (enviados por el operador) ──────────
            # Los operadores envían #abdiel, #grace o #bot desde su WhatsApp.
            # Estos mensajes llegan con from_me=True (enviados desde el número del negocio).
            if msg.es_propio:
                comando = detectar_comando_control(msg.texto)
                if comando in COMANDOS_PAUSA:
                    # Borrar el mensaje del chat ANTES de procesar,
                    # para que el cliente nunca vea el comando interno.
                    await proveedor.eliminar_mensaje(msg.mensaje_id)
                    operador = comando.lstrip("#")
                    await pausar_conversacion(msg.telefono, operador)
                    logger.info(f"Control humano activado por {operador} en {msg.telefono}")
                elif comando == COMANDO_REANUDAR:
                    # Borrar también el #bot del chat
                    await proveedor.eliminar_mensaje(msg.mensaje_id)
                    await reanudar_conversacion(msg.telefono)
                    logger.info(f"Bot retoma control de {msg.telefono}")
                # Ignorar todos los demás mensajes propios
                continue

            # ── Mensaje del cliente ───────────────────────────────────────────
            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # Si la conversación está en manos de un operador, el bot no interviene
            if await esta_pausada(msg.telefono):
                logger.info(f"Conversacion {msg.telefono} en control humano — bot silenciado")
                continue

            # Obtener historial ANTES de guardar el mensaje actual
            historial = await obtener_historial(msg.telefono)

            # Generar respuesta con Claude
            respuesta = await generar_respuesta(msg.texto, historial)

            # Guardar mensaje del usuario Y respuesta del agente en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta por WhatsApp via el proveedor
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
