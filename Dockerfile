# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Original sqlglot source at the pinned commit (allowed download). The
# selector applies each patch to these files to see exactly what changed.
ARG COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; \
t=tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${COMMIT}.tar.gz').read())); \
p='sqlglot-${COMMIT}/'; \
t.extractall('/tmp/x', members=[m for m in t.getmembers() if m.name.startswith(p+'sqlglot/') and m.name.endswith('.py')], filter='data')" \
 && mkdir -p /app/src && mv /tmp/x/sqlglot-${COMMIT}/sqlglot /app/src/sqlglot && rm -rf /tmp/x

COPY selector.py change_analysis.py /app/
COPY maps /app/maps
CMD ["python", "/app/selector.py"]
