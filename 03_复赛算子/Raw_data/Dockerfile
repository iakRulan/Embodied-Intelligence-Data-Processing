# Optional container image for platforms that run operators as containers.
FROM python:3.11-slim
WORKDIR /opt/refsync_qa
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# docker run -v <data>:/data/input -v <out>:/data/output refsync-qa
ENV RSQA_INPUT=/data/input RSQA_OUTPUT=/data/output
ENTRYPOINT ["python", "run.py"]
