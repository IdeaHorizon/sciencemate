# 参考文献

- 每条 BibTeX 必须有可解析的 doi 或 arXiv 编号或 url；三者都没有的条目不写。
- 只写你确定存在的文献。作者、年份、题名、刊物必须与真实出版物一致；不确定的宁可不写。
- key 用 `作者姓小写年份关键词`，如 `mensh2017rules`。
- 类型：期刊 @article，会议 @inproceedings，预印本 @misc 加 eprint/eprinttype=arXiv，软件与网页 @misc 加 url 与 urldate。
- 中文文献用 @article 并给 language={zh}。
- 每篇文献必须在正文里被 \cite 至少一次；正文里出现的 key 必须在这个文件里。
- 引言的文献要「说它们做到了什么」，不要成串挂尾。
