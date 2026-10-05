"""Explicit browsing commands, with quotes; no shared DTO or payment changes."""
import re
from app.contracts.search import Criterion


def price_order(text):
    patterns = [
        (r"价格越高越好|越贵越好|最贵|价格(?:从高到低|降序)|按价格(?:从高到低|降序)|most expensive|highest price|price high to low", "higher"),
        (r"价格越低越好|越便宜越好|最便宜|价格(?:从低到高|升序)|按价格(?:从低到高|升序)|cheapest|lowest price|price low to high", "lower"),
    ]
    found = [(m.start(), m.group(), direction) for pattern, direction in patterns
             for m in re.finditer(pattern, text, re.I)]
    if not found:
        return None
    _, quote, direction = max(found)
    return Criterion(attribute="price_cents", direction=direction, priority="high",
                     source="explicit", evidence_quote=quote)


def navigation(text):
    text = text.strip().strip("。，！？!? .")
    if re.fullmatch(r"(?:请|帮我)?(?:显示全部|显示所有商品|查看全部|查看全部商品|看全部|show all|all products)", text, re.I):
        return "all"
    if re.fullmatch(r"(?:请|帮我)?(?:换一批(?:看看)?|换几款|下一批|下一页|查看更多|查看更多商品|更多商品|看看其他的|看看其他商品|还有(?:别的|其他的)(?:耳机|商品)?吗|有没有(?:别的|其他的)(?:耳机|商品)?|再推荐几款|还有哪些|more|next page|next batch|show more)(?:耳机|商品|吧|呢)?", text, re.I):
        return "more"
    return None
