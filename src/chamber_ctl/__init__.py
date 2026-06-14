import os

ECS_IP = "10.194.210.21" #os.environ.get("ECS_HOST", "127.0.0.1")
ECS_PORT = int(os.environ.get("ECS_PORT", 11750))

print(f"ECS_IP: {ECS_IP}, ECS_PORT: {ECS_PORT}")