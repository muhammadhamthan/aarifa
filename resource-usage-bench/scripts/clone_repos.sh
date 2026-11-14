#!/usr/bin/env bash
# Shallow-clone the source of every platform the bench tested into resource-usage-bench/repos/ (git-ignored),
# at the version that was measured when a matching tag exists (else the default branch), and write
# repos/SOURCES.md: URL, ref asked for, ref cloned, commit, licence file and its first line, size on disk.
#   bash resource-usage-bench/scripts/clone_repos.sh
set -u
cd "$(dirname "$0")/../repos" || exit 1
git config --global core.longpaths true >/dev/null 2>&1 || true

# name | url | tag to try (version measured; "-" = default branch)
LIST='
asterisk|https://github.com/asterisk/asterisk|23.5.0
freeswitch|https://github.com/signalwire/freeswitch|v1.11.3
postal|https://github.com/postalserver/postal|3.3.4
haraka|https://github.com/haraka/Haraka|-
stalwart|https://github.com/stalwartlabs/stalwart|v1.1.0
text-embeddings-inference|https://github.com/huggingface/text-embeddings-inference|v1.8.3
FlagEmbedding|https://github.com/FlagOpen/FlagEmbedding|-
phoenix|https://github.com/Arize-ai/phoenix|arize-phoenix-v21.1.0
langfuse|https://github.com/langfuse/langfuse|-
opik|https://github.com/comet-ml/opik|-
bunkerweb|https://github.com/bunkerity/bunkerweb|v1.6.6
crowdsec|https://github.com/crowdsecurity/crowdsec|v1.7.3
SafeLine|https://github.com/chaitin/SafeLine|v9.2.7
coreruleset|https://github.com/coreruleset/coreruleset|v4.20.0
altcha|https://github.com/altcha-org/altcha|-
faster-whisper|https://github.com/SYSTRAN/faster-whisper|v1.2.1
whisper.cpp|https://github.com/ggml-org/whisper.cpp|-
whisper|https://github.com/openai/whisper|-
sherpa-onnx|https://github.com/k2-fsa/sherpa-onnx|-
piper1-gpl|https://github.com/OHF-Voice/piper1-gpl|v1.8.0
chatterbox|https://github.com/resemble-ai/chatterbox|-
pipecat|https://github.com/pipecat-ai/pipecat|-
sipp|https://github.com/SIPp/sipp|-
'

{
  echo "# Cloned sources"
  echo
  echo "Shallow clones (\`--depth 1\`) made by \`scripts/clone_repos.sh\` on $(date -u +%Y-%m-%d). \"Tag asked\" is the"
  echo "version the bench measured; when no such tag exists the default branch was cloned (\"ref cloned\" says which)."
  echo "LiveKit server and agents were cloned earlier at the measured versions (see README.md)."
  echo
  echo "| Repo | URL | Tag asked | Ref cloned | Commit | Licence file: first line | Size |"
  echo "|---|---|---|---|---|---|---|"
} > SOURCES.md.new

echo "$LIST" | while IFS='|' read -r name url tag; do
  [ -z "$name" ] && continue
  if [ ! -d "$name/.git" ]; then
    if [ "$tag" != "-" ] && git clone -q --depth 1 --branch "$tag" "$url" "$name" 2>/dev/null; then
      :
    else
      rm -rf "$name" 2>/dev/null
      git clone -q --depth 1 "$url" "$name" 2>/dev/null || { echo "| $name | $url | $tag | **clone failed** | | | |" >> SOURCES.md.new; continue; }
    fi
  fi
  ref=$(git -C "$name" describe --tags --exact-match 2>/dev/null || git -C "$name" rev-parse --abbrev-ref HEAD)
  commit=$(git -C "$name" rev-parse --short=12 HEAD)
  # licence file at the repo root: LICENSE*, LICENCE*, COPYING*, MIT-LICENCE ...; a LICENSES/ folder lists its files
  lic=$(ls "$name" | grep -iE 'licen[cs]e|^copying' | head -1)
  first=""
  if [ -n "$lic" ] && [ -d "$name/$lic" ]; then
    first="folder with $(ls "$name/$lic" | tr '\n' ' ' | cut -c1-70)"
  elif [ -n "$lic" ]; then
    first=$(grep -m1 -v '^[[:space:]]*$' "$name/$lic" | tr -d '|' | sed 's/^[[:space:]]*//' | cut -c1-70)
  fi
  size=$(du -sh "$name" 2>/dev/null | cut -f1)
  echo "| $name | $url | $tag | $ref | $commit | ${lic:-none}: ${first} | $size |" >> SOURCES.md.new
  echo "done $name ($ref, $size)"
done
mv SOURCES.md.new SOURCES.md
