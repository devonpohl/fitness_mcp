FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements-remote.txt .
RUN pip install --no-cache-dir -r requirements-remote.txt

# Copy application code
COPY fitness_mcp.py .
COPY deploy/ deploy/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# /data is where the Railway volume mounts.
# mkdir here so local Docker runs don't fail before a volume exists.
RUN mkdir -p /data

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
