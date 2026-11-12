# -*- coding: utf-8 -*-

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime
from collections import Counter
from openai import OpenAI

MODEL="qwen-plus-latest"
TRIALS=10


def load_project_env() -> None:
    """读取仓库根目录的 .env。已经导出的环境变量优先，不覆盖。"""
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return


load_project_env()

client = OpenAI(
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url=os.environ["base_url"],
    timeout=30.0,
    max_retries=0,
    
)


def sample(temperature: float, top_p: float) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Hello, world!"}],
        temperature=temperature,
        top_p=top_p,
        extra_body={"thinking": {"type": "disabled"}},
        max_tokens=16,
    )
    return response.choices[0].message.content.strip()

def run_case(*, label: str, temperature: float, top_p: float) -> None:
    values = [sample(temperature=temperature, top_p=top_p) for _ in range(TRIALS)]
    counts = Counter(values)
    print(f"[{label}] temperature={temperature}, top_p={top_p}")
    print(f"样本: {values}")
    print(f"去重数: {len(counts)}/{TRIALS}")
    print("Top 频次:", counts.most_common(5))
    print("-" * 50)

def main() -> None:
    print("=== 固定 top_p=1.0，只观察 temperature ===")
    for temperature in [0.0, 0.3, 0.8, 1.3]:
        run_case(label=f"temperature={temperature}", temperature=temperature, top_p=1.0)
    print("\n=== 固定 temperature=1.0，只观察 top_p ===")
    for top_p in [1.0, 0.7, 0.3, 0.1]:
        run_case(label=f"top_p={top_p}", temperature=1.0, top_p=top_p)

if __name__ == "__main__":
    main()