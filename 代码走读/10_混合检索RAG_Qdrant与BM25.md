# 10 · 混合检索 RAG · Qdrant 向量 + BM25 + RRF 融合 + Rerank

> 目标：看懂"客户问题被 rewrite 之后"的那一步——知识到底是怎么被"找"出来的。
> 覆盖 retrieval.py（混合检索器）、vector_store.py（Qdrant 封装）、build_index.py（建索引）、
> ingest.py（切块）、test_retrieval.py（纯函数测试）、eval_results/eval_retrieval.json（消融成绩）。
> 顺带点破三件"与直觉不符"的事：BM25 的 idf 乘了两次、建索引脚本会连带清空用户偏好集合、
> rerank_enabled 的注释把 RRF 的成绩说高了。

---

## 一、先建立心智模型：一次 retrieve() 内部干了四件事

00 步的一页速览里，链路是 ① rewrite → ② 混合检索 → ③ Supervisor 路由。
本步讲 ②。在主图里它对应 `context_retriever` 节点（agent.py:221-235）：拿 rewrite 结果
调 `retriever.retrieve(query, top_k=5, use_rerank=rerank_enabled())`，把命中的正文塞进
`knowledge_chunks` 喂给 Agent 当参考（agent.py:232）。

**为什么"找知识"要四条腿走路？** 看一条真实查询："面料起球怎么办"：
- **向量检索**：把句子编码成 768 维向量，找"语义上最像"的段落——它懂"起球 ≈ 起毛起球"，
  即使句子里一个精确词都不重合也能命中；
- **BM25**：数关键词命中的字面次数——"起球"俩字精确命中"常见问题-起毛起球"那条，词汇
  表外的改写对它无效，但精确词它从不放跑；
- **RRF 融合**：把两路各自的**排名**合并成一张表（不是分数相加，理由见 6.2）；
- **Rerank**：CrossEncoder 逐对精排，把融合后前 10 条里真正相关的提到最前（默认关）。

| 算法 | 强项 | 弱项 | 类比 |
|---|---|---|---|
| 向量（dense） | 语义近义、能"意会" | 精确词不敏感、幻觉邻居 | 靠"印象"认人 |
| BM25（稀疏） | 精确词/术语 100% 命中 | 无语义，同义改写就抓瞎 | 认工牌上的全名 |
| RRF | 免标定融合两路 | 只看排名、丢分数信息 | 两个评委按名次投票 |
| Rerank | 交叉精排、质量最高 | 贵（~1GB 模型 / API）、慢 | 终审专家逐份看 |

评测成绩先说结论（eval_results/eval_retrieval.json，85 条问句、Hit@3/MRR）：

| 配置 | Hit@3 | MRR |
|---|---|---|
| ① 纯向量 | 82.35% | 0.776 |
| ② 纯 BM25 | 95.29% | 0.835 |
| ③ 混合（RRF） | 96.47% | 0.849 |
| ④ 混合 + Rerank | **100%** | **0.959** |

`③` 只比 `②` 高 1.2 个点（向量那一路主要补 BM25 抓不到的口语问法），
`④` 才把最后的 3.5% 尾巴和 MRR（排序质量）拉满——这组数字直接解释了两个默认值：
RRF 必须做、Rerank 默认关（见 6.4）。

## 二、向量端：vector_store.py（Qdrant，LocalMode / 独立服务一份代码）

### 2.1 模式切换就一行判断（vector_store.py:37-49）

```python
url = os.getenv("QDRANT_URL")
if url:
    _client = QdrantClient(url=url)          # 独立 Qdrant 服务（生产）
else:
    path = str(os.path.join(PROJECT_ROOT, "index", "qdrant_storage"))
    _client = QdrantClient(path=path)        # LocalMode 本地嵌入式（开发）
```

设了 `QDRANT_URL` 就连服务，没设就在 `index/qdrant_storage` 目录里开本地实例——同一套
`ensure_collections` / upsert / query 代码两种模式通吃（模块注释 vector_store.py:3-8）。
LocalMode 是嵌入式（进程内、单写者、数据落目录），独立服务才有并发/过滤/水平扩展，
这也是从 ChromaDB 迁到 Qdrant 的动机（"嵌入式 → 可独立部署"）。

### 2.2 模型与集合

- embedding 模型：`EMBED_MODEL = "BAAI/bge-base-zh-v1.5"`（vector_store.py:26），
  维度 `VECTOR_SIZE = 768`（:27）。**embedding 在客户端算**（`_embedder()` 懒加载
  sentence_transformers，:52-57），Qdrant 只存向量和 payload——注释点明
  "embedding 仍在客户端（本地 bge-base-zh），Qdrant 只存向量与 payload"（:11）。
- 集合名：知识库 `"textile_knowledge"`、用户偏好 `"user_memory"`（:30-31），
  COSINE 距离建集合（:80）。
- 同步客户端塞回事件循环：Qdrant 客户端是同步的，所有调用套 `asyncio.to_thread`
  （`_run`，:60-62），rerank 推理同理（retrieval.py:242），**不让阻塞代码卡住事件循环**。

### 2.3 写入与检索

```python
async def upsert_knowledge(items):                 # :98-122
    ...
    vectors = _embedder().encode(texts, ...).tolist()
    points = [models.PointStruct(id=i + 1, vector=vectors[i],
              payload={"text": ..., "category": ..., "tags": ..., "title": ...}) ...]
    for i in range(0, len(points), 100):           # 分批，单请求 payload 有上限
        await _run(client.upsert, collection_name=KNOWLEDGE_COLLECTION, points=points[i:i + 100])
```

点 id 用**序号 i+1**（build_index 每次全量重建 → 顺序稳定）。检索侧：

```python
async def search_knowledge(query_text, category=None, top_k=5):   # :150-168
    vector = _embedder().encode([query_text], ...).tolist()[0]     # 每次查询都现场编码
    query_filter = (models.Filter(must=[FieldCondition(key="category", match=...)]) if category else None)
    resp = await _run(client.query_points, collection_name=KNOWLEDGE_COLLECTION,
                      query=vector, query_filter=query_filter, limit=top_k, with_payload=True)
    return [{"text": ..., "category": ..., "score": p.score} for p in resp.points]
```

注意 Qdrant 返回的 `p.score` 在 COSINE 下是**相似度，越大越近**。可选项 `category`
过滤在 Qdrant 侧用 payload filter 完成（不再全量拉回来自己滤——这是比 ChromaDB 时代的
演进，retrieval.py:8 自述）。

## 三、BM25 端：自研稀疏索引（不依赖任何向量库）

### 3.1 中文分词：英文整词 + 汉字 bigram（retrieval.py:65-73）

```python
def tokenize(text: str) -> List[str]:
    text = text.lower().strip()
    for match in re.finditer(r"[a-zA-Z0-9]+", text):   # "T400"、"FDY" 这类整词保留
        tokens.append(match.group())
    chars = re.findall(r"[一-鿿]", text)                # 只留汉字
    for i in range(len(chars) - 1):                    # 相邻两字拼成一个 token
        tokens.append(chars[i] + chars[i + 1])
```

"涤塔夫" → "涤塔"+"塔夫"，"色差" 原样成词——bigram 是**免分词器的廉价方案**（不用
jieba 等依赖，领域词典零成本），测试 test_retrieval.py:9-13 断言了"黑色弹力"必须切出
"黑色/色弹/弹力"。

### 3.2 BM25 打分公式（retrieval.py:107-130）

```python
for token in query_tokens:
    idf = self.idf.get(token, 0.0)
    ...
    tf = self.doc_tokens[idx].count(token)
    ...
    num = tf * (self.k1 + 1)
    den = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
    score += idf * idf * num / den          # ← 注意 idf 乘了两次！
```

- `k1=1.5, b=0.75` 是 BM25 经典默认（retrieval.py:80）；
- **与教科书不符的点**：标准 BM25 是 `idf * tf*(k1+1)/...`，这里写成 `idf * idf * ...`。
  且 idf 公式也加了平滑：`log((N-f+0.5)/(f+0.5) + 1.0)`（retrieval.py:102-105）。由于
  建索引脚本用的是同一份公式（build_index.py:110-133），两边一致，实际效果是"稀有词
  权重被二次放大"，测试只断言相对排序（test_retrieval.py:16-20），所以没暴露——属于
  "跑得对但写法非主流"，面试主动指出是加分的细节眼力；
- 索引文件：`index/bm25_index.pkl`（retrieval.py:58），`BM25Index.load`（:132-143）把
  documents / doc_tokens / 各项统计 pickle 读回——**分词结果在建索引时就固化进 pkl**，
  检索时不重切文档，毫秒级。

## 四、RRF 融合：只认名次不认分数（retrieval.py:149-166）

```python
def rrf_fusion(vector_results, bm25_results, k: int = 60, vec_weight: float = 0.5):
    scores: Dict[str, float] = {}
    bm25_weight = 1.0 - vec_weight          # 向量:BM25 = 0.5:0.5
    for rank, (text, idx, _) in enumerate(vector_results):
        scores[text] = vec_weight / (k + rank + 1)     # 向量路：0.5/(60+名次)
    for rank, (text, idx, _) in enumerate(bm25_results):
        scores[text] = scores.get(text, 0.0) + bm25_weight / (k + rank + 1)  # BM25 路：累加
    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(text, -1, score) for text, score in fused]
```

- 第 1 名得 `0.5/61`、第 2 名 `0.5/62`……**名次越靠前分越高，两路都命中就累加**——
  这正是 test_retrieval.py:29-34 的断言：docB 向量、BM25 双路命中，胜过只中一路的 docA；
- **RRF 的设计精髓：只吃排名，不吃分数**。cosine 相似度和 BM25 分数是两个世界的东西
  （一个 [−1,1] 一个无上界），直接相加要标定权重；换算成名次后天然可比，`k=60` 是让
  高分差被摊平的平滑常数（rank 0~59 内差距被 60 稀释）；
- 融合前两路各自先取 top 10（`vec_k=10, bm25_k=10`，retrieval.py:179-180）——召回
  **宁可多给候选，让 Rerank 去精筛**：`fetch_k = max(top_k, 10) if use_rerank else top_k`
  （retrieval.py:228），不开 rerank 时取 top_k 即停（省钱），开了就多留到 10 条喂重排器。

## 五、Rerank：CrossEncoder 终审（默认关）

```python
def rerank_enabled() -> bool:               # retrieval.py:32-39
    """Rerank 总开关：RERANK_ENABLED=1 才开启。...默认关闭"""
    return os.getenv("RERANK_ENABLED", "0") == "1"

_reranker = None
def _get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder("BAAI/bge-reranker-base", max_length=512)   # :50
    return _reranker
```

- 模型 `BAAI/bge-reranker-base`、**懒加载**（首次真用才下/才加载，import 不卡）；
- `_rerank_sync`（retrieval.py:251-260）：把 query 和每条候选拼成 `[query, 正文前500字]`
  对，`predict` 打分后按分数倒序截 top_k——CrossEncoder 是 query 与 doc **交叉编码**，
  比双塔向量更能抓细粒度相关性；
- 调用点用 `asyncio.to_thread` 包住（retrieval.py:242），推理不阻塞事件循环；
- 在 retrieve 里还守了一道：`if use_rerank and len(results) > top_k`（:241）——候选不够
  多就不白花一次模型推理。

## 六、一次 retrieve() 的完整内部顺序 + 返回结构

以 `context_retriever` 的调用 `retrieve(query, top_k=5, use_rerank=rerank_enabled())`
（agent.py:231）为例，retrieval.py:199-249：

```
0. _ensure_text2cat()：懒构建"正文→类别"映射
   （首次检索时 Qdrant scroll 全量拉一遍，retrieval.py:263-285；失败降级为空映射 :194-196）
1. 向量路：search_knowledge(query, top_k=10) → 把相似度转成"距离"占位 (text, idx, 1.0-score)
2. BM25 路：self.bm25.search(query, top_k=10)
   → 若指定 category，用 text2cat 映射过滤（retrieval.py:223-224）
3. RRF 融合（vec_weight=0.5）
4. 取前 fetch_k 组装 [{text, score(4位), category}]
5. 若 use_rerank 且候选 > top_k → to_thread(_rerank_sync) 精排截断
```

返回 `[{"text": str, "score": float, "category": str}, ...]`（rerank 后多一个
`rerank_score` 字段，:258）。调用方只取 `text` 拼进知识上下文（agent.py:232-234）——
检索的"得分"不直接暴露给 LLM，只给它正文。

> 与直觉不符的细节①：向量路的 `1.0 - score`（retrieval.py:216）看着像把相似度转距离，
> 但 RRF **只用名次**，这个转换数值上根本没参与计算——是历史遗留的无害写法。
> 与直觉不符的细节②：`_hybrid`（eval 脚本里 ③ 的实现，eval_retrieval.py:156-162）给
> 向量路塞的是常数 1.0 而不是真实相似度，结果一样——再次证明 RRF 对分数免疫。

## 七、数据与索引是怎么来的：ingest.py → chunks.json → build_index.py

### 7.1 ingest.py：原始文档 → 结构化 chunk（元数据与正文分离）

输入两路（ingest.py:33-34）：`data/raw/*.md`（新，按 markdown 标题切）和
`data/knowledge.txt`（旧，`---` 分隔块，文件头注释自述 200-500 字/块，knowledge.txt:2）。
knowledge.txt 的块格式：

```
[类别] 面料基础-涤塔夫          ← 元数据
[标签] 涤塔夫 涤丝纺 规格 ...
【涤塔夫（涤丝纺）】            ← 标题
成分：100%涤纶...               ← 正文（干净，不含标记）
```

parse_old_kb（ingest.py:42-74）把标记剥掉，但**标题并回正文头部**
（`text = f"{t}\n{body}"`，:67）——注释点破原因："标题（如'腈纶'）是可检索内容，
剥离会丢关键词"。同理 markdown 路线的 category 是"大类-小类"两级
（`category = f"{current_cat}-{current_title}"`，:96）。去重规则（:204-218）：按
"大类 + 规范化标题"（去括号、只留中文字符）近似去重，正文更长的保留。打标签默认走
**规则法**（rule_tags，:121-137：标题拆词 + 括号别名 + 大类名），`--llm` 才调 DeepSeek
自动打标签（llm_tags，:152-171）。输出 chunks.json，当前 142 条，字段
`{text, category, tags, title}`（data/chunks.json 头部可验）。

### 7.2 build_index.py：chunks.json → Qdrant 向量 + BM25 pkl

```python
async def build_qdrant(chunks):
    print("   🗑️ 清空旧向量集合...")
    reset_collections()          # build_index.py:64 —— 先全量清空
    await upsert_knowledge(chunks)   # 再全量重写（点 id = 1..N）
def build_bm25(chunks):
    bm25.index(texts); bm25.save(BM25_PATH)    # :154-160 覆盖写 index/bm25_index.pkl
```

**幂等性**：每次构建 = 清空 + 全量重建 + pkl 覆盖写，所以重复跑结果一致、不会越跑越多
（数据源变了重跑一次即可，Q5）。BM25 那套 BM25Index 类在 build_index.py:84-152 又写了
**一份**（和 retrieval.py:79-143 双份实现，靠 pickle 字段契约对齐——维护隐患，Q6）。

**与直觉不符的坑**：`reset_collections`（vector_store.py:85-90）清空的是
**两个** collection——`textile_knowledge` **和 `user_memory`**。它本是给测试用的
（docstring："清空集合（建索引/测试用）"），却被 build_index 的生产路径复用了：
**跑一次建索引脚本，会把所有用户偏好（Qdrant 里的长期记忆）一起删掉**，之后靠运行期的
ensure_collections 重建一个空集合。数据量小所以没人发现，但这是典型的"测试函数被生产
路径复用"事故原型——面试提出来很加分。

## 八、评测怎么跑、数字怎么读（scripts/eval_retrieval.py）

- 评测集：85 条"问句 → 期望命中的类别"，覆盖面料名/语义场景/工艺/口语（:31-134 注释
  自述题型混合是刻意为之——"精确面料名（向量易漏、BM25 能救）+ 语义场景"）；
- 判定口径：**类别命中**而非"某一段文字命中"——top-k 里出现期望的 category 就算 Hit
  （eval_retrieval.py:175-179）。因为是问答，系统只在乎"正确主题有没有进上下文"，这是
  务实口径（Q4 展开）；
- 四种配置（:192-197）：纯向量直接调 search_knowledge；纯 BM25 直接查索引再查 text2cat
  映射；RRF 与线上 retrieve 同构；rerank 走 `retrieve(use_rerank=True)`。**评测显式传
  use_rerank，不受 RERANK_ENABLED 环境开关影响**（retrieval.py:37 注释）——开关只管
  线上默认行为；
- 结论注释（:208-214）：Hit@3 已接近饱和，**拿 MRR 说话**更公平（排序质量）；
  纯 BM25 MRR 0.835 优于纯向量 0.776（领域术语密集，字面命中比语义更可靠），
  RRF 抬到 0.849，加 Rerank 到 0.959。

---

## Q&A

**Q1：既然有向量检索，为什么还要 BM25 / 混合？评测数据怎么证明？**
看 eval_retrieval.json 就能答：纯向量 Hit@3 只有 82.35%（85 条里有 15 条没进前三），
纯 BM25 却到 95.29%。原因在评测集的刻意设计上：领域问题大量是**精确术语**（"FDY DTY
ATY 区别"、"塔夫绸是什么"），bge-base-zh 这类通用 embedding 把"塔夫绸"和"涤塔夫"编码
得很近，会召回语义邻居却漏掉字面精确的那条；BM25 对术语词 100% 字面命中，但完全不懂
"起球"≈"起毛起球"这种同义改写。两者失败模式互补 → 融合后 96.47%，Rerank 再把排序做
精到 100%/0.959。一句话：**知识库问答里"精确词"和"语义"缺一不可，这是用数据选出来的
架构**，不是拍脑袋。

**Q2：LocalMode 和独立 Qdrant 服务怎么切？各图什么？**
切换只看一个环境变量：`QDRANT_URL` 设置了就连 `QdrantClient(url=url)`（生产独立服务，
支持并发/多实例/过滤），没设置就 `QdrantClient(path=index/qdrant_storage)`（LocalMode
嵌入式，vector_store.py:41-48）。取舍：LocalMode 零运维、进程内、数据落在项目目录，适合
开发/演示/单进程；但它本质是单写者嵌入式存储，多实例部署时会互相踩数据文件，所以生产
必须独立服务。设计亮点是"一份业务代码两种模式"——切换只改环境变量不动代码，测试和
CI 也能用 LocalMode 跑。另注意 embedding 始终在客户端算（vector_store.py:11 注释），
Qdrant 只当向量仓库——这样换 embedding 模型不用动数据库。

**Q3：Rerank 效果最好，为什么默认关（RERANK_ENABLED=0）？**
retrieval.py:32-39 的注释把账算明白了："本地 CrossEncoder ~1GB、或走重排 API 都要花钱/
内存；默认关闭，混合检索本身……"，加上 `_get_reranker` 注释（:42）"模型 ~1GB，首次
rerank 时才加载"。即：每条用户消息若都 rerank，要么本地扛 ~1GB 模型 + 每问一次
CrossEncoder 推理（10 对打分，本地 CPU 能到几百毫秒~秒级），要么花 API 钱；而关闭它
线上仍有 96.47% Hit@3——**为尾部 3.5% 的命中率提升付每条消息的延迟/成本，不划算**。
所以开关留给部署者按预算决定（线上数据密集行业版可开）。另外评测脚本显式传
use_rerank（retrieval.py:37），不受开关影响——开关是"线上默认"概念，不是"能力"概念。

**Q4：rerank_enabled 的注释说"RRF 本身 Hit@3 已达 100%"，但评测 json 里 RRF 只有
96.47%，矛盾吗？**
矛盾，且值得较真：retrieval.py:36 写"默认关闭，混合检索（向量+BM25+RRF）本身 Hit@3 已
达 100%"——但 eval_results/eval_retrieval.json 里 ③ 混合(RRF) 是 **96.47%**，只有 ④
混合+Rerank 才到 100%。注释说高了（可能是某次更小评测集的结论被写进了注释）。另一个
口径因素：评测的 Hit@3 是**"类别命中"**（eval_retrieval.py:175-179：top-3 里出现期望
category 即算中），不是逐句命中——类别级指标天然更容易满分。面试这样答："注释和数据
打架，以数据为准：RRF 96.47%，Rerank 才 100%；而且口径是类别命中"。主动指认代码注释
与实验数据的出入，比照着注释背结论可信得多。

**Q5：改了知识库（data 源）之后，索引怎么更新？会不会不一致？**
知识改了 → 重跑 `python scripts/ingest.py`（重新切块出 chunks.json）→
`python scripts/build_index.py`（Qdrant 向量 + BM25 pkl 一起重建）。两个脚本都**幂等**：
ingest 去重合并后整体覆盖写 chunks.json；build_index 先 `reset_collections()` 清空向量库
再全量 upsert（build_index.py:61-66），BM25 索引整体 pickle 覆盖（:154-160）——不会出现
"旧条目残留 + 新条目追加"的脏增量。查询侧 HybridRetriever 启动时从磁盘 load BM25
（retrieval.py:178），向量侧检索前 ensure_collections；**改了数据不重跑脚本**的后果是
新旧混杂或查不到新条目（尤其 BM25 是磁盘 pkl，不重跑永远旧）。两个隐患要主动讲：
① 建索引会连带清空 user_memory 用户偏好集合（七节末的坑）；② 检索端 BM25 路径要
`_load_text2cat` 从 Qdrant 现拉映射，若索引与 chunks.json 不同步，BM25 命中的正文在
映射里查不到类别会标"未知"（retrieval.py:237 兜底）。

**Q6：为什么 BM25 的 idf 乘了两次？检索器和建索引脚本为什么有两份实现？**
idf 平方：检索端 `score += idf * idf * num / den`（retrieval.py:126），建索引端同样
`idf * idf`（build_index.py:133）——标准 BM25 只乘一次。效果是稀有词的区分度被二次
放大，由于两处公式一致 + 测试只断言排序相对关系（test_retrieval.py:16-20），行为稳定、
没暴露为 bug，属"跑得对但非主流写法"（历史手写演进遗留）。双份实现：retrieval.py 的
BM25Index（79-143）与 build_index.py 的 BM25Index（84-152）几乎逐行重复，靠 pickle 里
的字段（documents/doc_tokens/avgdl/idf/N/k1/b）对齐契约——将来改分词或打分公式要记得
两处同步改，否则"建出来的索引"和"查索引的代码"悄悄分叉（这正是 idf² 至今没被"修正"
的原因之一：动了检索端不动建索引端，线上立刻坏）。重构方向：把 BM25 抽成共享模块，建
索引与检索 import 同一份。

**Q7：向量检索会不会是"搜索热点"瓶颈？本地模式每次查询都要现场 encode 吗？**
是。search_knowledge 每次查询都 `_embedder().encode([query_text])` 现场编码（vector_store.py:154），
embedding 模型 bge-base-zh-v1.5 加载后常驻内存（懒加载单例 :52-57），单条查询编码本身
很快（毫秒~几十毫秒级），但**没有 query 向量缓存**——同义改写过的热门问句每次全链路
重算。这是演示量级的取舍：加一层"查询改写 → 向量"的 LRU 缓存即可优化，但知识库只有
142 条（data/chunks.json），BM25 路径毫秒级返回，向量路径也远快于 LLM 生成，所以没做。
追问点：知识量级上去后（万条级）瓶颈会先出现在 `_load_text2cat` 的全量 scroll
（retrieval.py:263-285，每次进程首次检索都拉一遍全集）——那才是该缓存/该改成 payload
索引的地方。

**Q8（追问）：Rerank 为什么不直接对所有候选跑，而要 RRF 先筛到 10 条？**
成本结构决定的：CrossEncoder 是"每对 (query, doc) 一次前向"，候选 10 条要 10 次前向、
100 条要 100 次。所以两段式：**粗排召回**（向量 top10 + BM25 top10 → RRF 融合去重，
免费毫秒级）把候选压到 10 条以内，**精排**（rerank）只在这 10 条里做（retrieval.py:228-242）。
这就是工业界标准的"召回-粗排-精排"漏斗在 142 条语料上的迷你版：先保证召回率（两路
top10 谁都不漏），再用最贵的模型保精度。另一个细节：喂给 rerank 的正文截断到 500 字
（retrieval.py:255 `doc["text"][:500]`）——CrossEncoder 有 max_length=512 的输入上限，
长文截尾是显式处理而不是碰运气。
