#!/usr/bin/env python3
"""测试 RAGFlow 外部检索接口"""

import requests

url = "http://192.168.1.3:9380/v1/api/retrieval"
headers = {
    "Authorization": "Bearer ragflow-gzMmRiOTEwNTc2ZDExZjE5YmYyMGVlMz",
    "Content-Type": "application/json;charset=utf-8",
}

# 示例请求体，可根据实际情况调整
payload = {
    "question": "测试问题",
    "kb_id": ['27b7e08e575311f1902a4e34f9d2d4f7'],
    "page_size": 5,
    "rerank_id": 'BAAI/bge-reranker-v2-m3@SILICONFLOW'
}

try:
    response = requests.post(url, headers=headers, json=payload, verify=False, timeout=30)
    data = response.json()
    # print(f"Status: {response.status_code}")
    # print(f"Response: {response.text}")
    # print(len(data['data']['chunks']))
    for chunk in data['data']['chunks']:
        print(chunk['chunk_id'])
except Exception as e:
    print(f"Error: {e}")