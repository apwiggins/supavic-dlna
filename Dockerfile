FROM python:3.11-slim

# Install system dependencies and git to clone/manage the repo
RUN apt-get update && apt-get install -y --no-install-recommends \
  git \
  build-essential \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone the repository
RUN git clone https://github.com/nomuraki/supavic-dlna.git .

# Install dependencies if requirements.txt or a setup file exists
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
RUN if [ -f pyproject.toml ] || [ -f setup.py ]; then pip install --no-cache-dir .; fi

# Copy the custom entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose potential DLNA / web control panel ports (adjust if needed)
EXPOSE 8080 1900/udp

ENTRYPOINT ["/entrypoint.sh"]
