# Qlar Email Gateway — single stage: every dependency is pure Python or ships a wheel, so
# there is no compiler to keep out of the final image.
FROM python:3.12-slim

RUN useradd --system --uid 10001 --create-home --home-dir /home/qlar qlar

COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src
RUN python -m venv /venv     && /venv/bin/pip install --no-cache-dir --upgrade pip     && /venv/bin/pip install --no-cache-dir /build     && rm -rf /build

ENV PATH="/venv/bin:$PATH"     PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     GATEWAY_KEY_FILE=/state/gateway-key.pem     GATEWAY_STATE_FILE=/state/gateway-state.json     AUDIT_LOG_FILE=/state/audit/mail.jsonl

# Identity, state and the audit log live on the volume so they survive container upgrades.
RUN mkdir -p /state && chown -R qlar:qlar /state
VOLUME ["/state"]

USER qlar
WORKDIR /home/qlar

# The gateway listens on no port: there is deliberately no EXPOSE.
ENTRYPOINT ["qlar-email-gateway"]
CMD ["run"]
