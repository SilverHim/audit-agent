#!/bin/zsh
# 渲染 docs/diagrams/*.puml -> docs/diagrams/out/{png,svg}
# 用法：docs/diagrams/render.sh [file.puml ...]   不带参数则渲染全部
set -e
cd "$(dirname "$0")"
JAR=~/.local/lib/plantuml.jar
COMMON=(-Djava.awt.headless=true -Dfile.encoding=UTF-8 -DPLANTUML_LIMIT_SIZE=32768)
FILES=("$@"); [[ ${#FILES} -eq 0 ]] && FILES=(*.puml)
for f in "${FILES[@]}"; do
  f=${f##*/}
  GRAPHVIZ_DOT=/opt/homebrew/bin/dot java -Xmx3g "${COMMON[@]}" -jar $JAR -tpng -Sdpi=200 -o out "$f"
  GRAPHVIZ_DOT=/opt/homebrew/bin/dot java "${COMMON[@]}" -jar $JAR -tsvg -o out "$f"
  echo "$f -> out/${f%.puml}.png / .svg"
done
