from .protocol import Paper
import math
from html import escape
from .journal_dates import FRESHNESS_LABELS


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    .star-wrapper {
      font-size: 1.3em; /* 调整星星大小 */
      line-height: 1; /* 确保垂直对齐 */
      display: inline-flex;
      align-items: center; /* 保持对齐 */
    }
    .half-star {
      display: inline-block;
      width: 0.5em; /* 半颗星的宽度 */
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
  </style>
</head>
<body>

<div>
    __CONTENT__
</div>

<br><br>
<div>
To unsubscribe, remove your email in your Github Action setting.
</div>

</body>
</html>
"""

def get_empty_html():
  block_template = """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No Papers Today. Take a Rest!
    </td>
  </tr>
  </table>
  """
  return block_template

def get_block_html(title:str, authors:str, rate:str, tldr:str, pdf_url:str, affiliations:str=None, link_label:str="PDF", metadata_html:str="", summary_label:str="TLDR"):
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
            {title}
            {metadata_html}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0;">
            {authors}
            <br>
            <i>{affiliations}</i>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>Relevance:</strong> {rate}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>{summary_label}:</strong> {tldr}
        </td>
    </tr>

    <tr>
        <td style="padding: 8px 0;">
            <a href="{pdf_url}" style="display: inline-block; text-decoration: none; font-size: 14px; font-weight: bold; color: #fff; background-color: #d9534f; padding: 8px 16px; border-radius: 4px;">{link_label}</a>
        </td>
    </tr>
</table>
"""
    return block_template.format(title=title, authors=authors,rate=rate, tldr=tldr, pdf_url=escape(pdf_url or "", quote=True), affiliations=affiliations, link_label=link_label, metadata_html=metadata_html, summary_label=escape(summary_label))

def get_stars(score:float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    else:
        interval = (high-low) / 10
        star_num = math.ceil((score-low) / interval)
        full_star_num = int(star_num/2)
        half_star_num = star_num - full_star_num * 2
        return '<div class="star-wrapper">'+full_star * full_star_num + half_star * half_star_num + '</div>'


def render_email(papers:list[Paper]) -> str:
    parts = []
    if len(papers) == 0 :
        return framework.replace('__CONTENT__', get_empty_html())
    
    for p in papers:
        #rate = get_stars(p.score)
        rate = round(p.score, 1) if p.score is not None else 'Unknown'
        author_list = [a for a in p.authors]
        num_authors = len(author_list)
        if num_authors <= 5:
            authors = ', '.join(author_list)
        else:
            authors = ', '.join(author_list[:3] + ['...'] + author_list[-2:])
        if p.affiliations is not None:
            affiliations = p.affiliations[:5]
            affiliations = ', '.join(affiliations)
            if len(p.affiliations) > 5:
                affiliations += ', ...'
        else:
            affiliations = '机构信息未提供' if p.source == "journals" else 'Unknown Affiliation'
        title = escape(p.title)
        details = []
        source = "已订阅期刊" if p.source == "journals" else p.source
        details.append("来源：" + escape(source))
        if p.journal:
            details.append("期刊：" + escape(p.journal))
        if p.online_date:
            details.append("在线发表：" + escape(p.online_date) + "；日期依据：" +
                           escape(p.publication_date_source or "来源未提供"))
        else:
            details.append("在线发表日期：待核实")
            if p.publication_date:
                details.append(escape(p.publication_date_kind or "参考日期（非在线发表）") + "：" +
                               escape(p.publication_date) + "（不作为最新在线发表依据）")
        if p.freshness:
            details.append("日期状态：" + escape(FRESHNESS_LABELS.get(p.freshness, "待核实")))
        metadata_html = '<div style="font-size:13px;font-weight:normal;color:#555;margin-top:8px">' + '<br>'.join(details) + '</div>'
        summary_label = {"generated": "AI 摘要", "abstract_fallback": "原文摘要（AI 生成失败）",
                         "unavailable": "摘要未提供", "original_abstract": "原文摘要（未请求 AI）"}.get(p.tldr_status, "摘要（生成状态未记录）")
        parts.append(get_block_html(
            title, escape(authors), rate, escape(p.tldr or ""), p.pdf_url or p.url,
            escape(affiliations), "PDF" if p.pdf_url else "期刊原文 / DOI",
            metadata_html=metadata_html, summary_label=summary_label,
        ))

    failures = sum(p.tldr_status == "abstract_fallback" for p in papers)
    notice = (f'<p style="color:#9b4d00">本次 {failures} 篇 AI 摘要生成失败，已明确标注并展示原文摘要；未将其作为 AI 总结。</p>'
              if failures else "")
    content = notice + '<br>' + '</br><br>'.join(parts) + '</br>'
    return framework.replace('__CONTENT__', content)
