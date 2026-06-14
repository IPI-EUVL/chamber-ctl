import os


ECS_IP = os.environ.get("ECS_HOST", "127.0.0.1")
ECS_PORT = int(os.environ.get("ECS_PORT", 11750))