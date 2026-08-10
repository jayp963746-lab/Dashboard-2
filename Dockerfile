FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py dashboard.py ./

# Bot connects out to Discord; dashboard listens on this port.
EXPOSE 5000

# main.py starts the dashboard in a background thread, then runs the bot —
# one process, one container.
CMD ["python", "main.py"]
