from ollama import chat
import os

print("OLLAMA_BASE_URL =", os.getenv("OLLAMA_BASE_URL"))

resp = chat(
    model="llama3.1:8b",
    messages=[{"role": "user", "content": "Say OK"}],
)

print(resp["message"]["content"])

