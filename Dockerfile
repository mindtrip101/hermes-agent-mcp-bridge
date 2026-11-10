FROM python:3.12-slim

WORKDIR /opt/bridge
COPY pyproject.toml README.md LICENSE ./
COPY hermes_agent_mcp_bridge ./hermes_agent_mcp_bridge
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin bridge \
    && mkdir -p /data \
    && chown bridge:bridge /data
USER bridge
ENV HERMES_BRIDGE_DATA_DIR=/data
EXPOSE 8765
ENTRYPOINT ["hermes-agent-mcp-bridge"]
CMD ["serve", "--config", "/config/config.yaml"]
