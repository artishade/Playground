FROM python:3.12-slim

WORKDIR /app

# Install system deps for Pillow/numpy if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY emulator_control_server.py .
COPY mcp_server.py .

EXPOSE 8080

# Default: run the emulator control server on port 8080
# The MCP server runs as a separate stdio process (started by the MCP client)
CMD ["python", "emulator_control_server.py"]