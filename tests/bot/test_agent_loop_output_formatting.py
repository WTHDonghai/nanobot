# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for final output formatting in AgentLoop."""

from vikingbot.agent.loop import AgentLoop


def test_normalize_final_output_text_collapses_multiple_blank_lines() -> None:
    content = """
第一段



第二段


![image1](send://image1.png)


第三段
""".strip()

    normalized = AgentLoop._normalize_final_output_text(content)

    assert normalized == (
        "第一段\n\n第二段\n\n![image1](send://image1.png)\n\n第三段"
    )
    assert "\n\n\n" not in normalized


def test_normalize_final_output_text_repairs_broken_send_image_tail() -> None:
    content = (
        "图2.5-2 VIP客人信息列表 团队：点击【团队】标签，即可查看团队信息列表，"
        "如图： ](send://bd57ec19e4d4434e825864ab8a303223.jpeg)image144 "
        "图2.5-3 团队信息列表"
    )

    normalized = AgentLoop._normalize_final_output_text(content)

    assert "![image144](send://bd57ec19e4d4434e825864ab8a303223.jpeg)" in normalized
    assert "](send://bd57ec19e4d4434e825864ab8a303223.jpeg)image144" not in normalized


def test_normalize_final_output_text_does_not_repair_send_images_inside_code_fences() -> None:
    content = """示例：

```md
](send://bd57ec19e4d4434e825864ab8a303223.jpeg)image144
```
"""

    normalized = AgentLoop._normalize_final_output_text(content)

    assert "![image144](send://bd57ec19e4d4434e825864ab8a303223.jpeg)" not in normalized
    assert "](send://bd57ec19e4d4434e825864ab8a303223.jpeg)image144" in normalized


def test_normalize_final_output_text_preserves_blank_lines_inside_code_fences() -> None:
    content = """
说明文字


```python
print("hello")


print("world")
```


补充说明
""".strip()

    normalized = AgentLoop._normalize_final_output_text(content)

    assert normalized == (
        '说明文字\n\n```python\nprint("hello")\n\n\nprint("world")\n```\n\n补充说明'
    )
