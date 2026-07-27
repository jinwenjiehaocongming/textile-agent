"""
SQLite 数据库操作练习
=====================
CRUD: Create(增) Read(查) Update(改) Delete(删)

运行: python test_2.py
"""

import sqlite3

# ============================================================
# 0. 基础：连接 + 游标
# ============================================================
# 打开连接 → 拿到游标 → 执行 SQL → 关闭
conn = sqlite3.connect("data/products.db")

# 设置 row_factory 才能用列名访问结果（否则只能用下标 r[0]）
conn.row_factory = sqlite3.Row

# ============================================================
# 1. 查 SELECT — WHERE / LIKE / ORDER BY / LIMIT
# ============================================================
print("=" * 50)
print("1. 查询练习")
print("=" * 50)

# 1.1 查全表
print("\n【1.1 全部产品】")
rows = conn.execute("SELECT id, name, price FROM products").fetchall()
for r in rows:
    print(f"  {r['id']} {r['name']:16s} ¥{r['price']}/米")

# 1.2 WHERE 条件筛选
print("\n【1.2 黑色面料】")
rows = conn.execute(
    "SELECT id, name, price, stock FROM products WHERE color = '黑色'"
).fetchall()
for r in rows:
    print(f"  {r['id']} {r['name']:16s} ¥{r['price']}/米 {r['stock']}米")

# 1.3 LIKE 模糊搜索
print("\n【1.3 名字含 T400 的产品】")
rows = conn.execute(
    "SELECT id, name, price FROM products WHERE name LIKE '%T400%'"
).fetchall()
for r in rows:
    print(f"  {r['id']} {r['name']:16s} ¥{r['price']}/米")

# 1.4 排序 ORDER BY
print("\n【1.4 按价格从便宜到贵】")
rows = conn.execute(
    "SELECT id, name, price FROM products ORDER BY price ASC"
).fetchall()
for r in rows:
    print(f"  {r['id']} {r['name']:16s} ¥{r['price']}/米")

# 1.5 LIMIT 限制条数
print("\n【1.5 最贵的三款】")
rows = conn.execute(
    "SELECT id, name, price FROM products ORDER BY price DESC LIMIT 3"
).fetchall()
for r in rows:
    print(f"  {r['id']} {r['name']:16s} ¥{r['price']}/米")

# 1.6 聚合 COUNT / AVG / MAX / MIN
print("\n【1.6 统计】")
stats = conn.execute("""
    SELECT
        COUNT(*) AS 总数,
        ROUND(AVG(price), 1) AS 均价,
        MAX(price) AS 最高价,
        MIN(price) AS 最低价,
        SUM(stock) AS 总库存
    FROM products
""").fetchone()
print(f"  共 {stats['总数']} 款产品")
print(f"  均价 ¥{stats['均价']}/米")
print(f"  最高 ¥{stats['最高价']}/米, 最低 ¥{stats['最低价']}/米")
print(f"  总库存 {stats['总库存']} 米")

# 1.7 GROUP BY 分组统计
print("\n【1.7 按颜色分组统计】")
rows = conn.execute("""
    SELECT color, COUNT(*) AS 款数, ROUND(AVG(price), 1) AS 均价
    FROM products
    GROUP BY color
""").fetchall()
for r in rows:
    print(f"  {r['color']}: {r['款数']}款, 均价¥{r['均价']}")

# ============================================================
# 2. 增 INSERT
# ============================================================
print("\n" + "=" * 50)
print("2. 新增练习")
print("=" * 50)

conn.execute("""
    INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days)
    VALUES ('P999', '测试面料', '化纤面料', '测试色', 150, '100D*100D', 100, 50, 9.9, 3)
""")
conn.commit()
row = conn.execute("SELECT * FROM products WHERE id = 'P999'").fetchone()
print(f"  新增: {row['name']} {row['color']} ¥{row['price']}/米")
print(f"  当前总数: {conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]}")

# ============================================================
# 3. 改 UPDATE
# ============================================================
print("\n" + "=" * 50)
print("3. 修改练习")
print("=" * 50)

conn.execute("UPDATE products SET price = 6.6 WHERE id = 'P999'")
conn.commit()
row = conn.execute("SELECT price FROM products WHERE id = 'P999'").fetchone()
print(f"  调价后: ¥{row['price']}/米")

# ============================================================
# 4. 删 DELETE
# ============================================================
print("\n" + "=" * 50)
print("4. 删除练习")
print("=" * 50)

conn.execute("DELETE FROM products WHERE id = 'P999'")
conn.commit()
count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
print(f"  删除后剩余: {count} 款")

conn.close()

# ============================================================
# 5. 速查表
# ============================================================
print("\n" + "=" * 50)
print("SQL 速查")
print("=" * 50)
print("""
查  SELECT 列 FROM 表 WHERE 条件 ORDER BY 列 LIMIT 数量
增  INSERT INTO 表 (列1, 列2) VALUES (值1, 值2)
改  UPDATE 表 SET 列 = 值 WHERE 条件
删  DELETE FROM 表 WHERE 条件

常用条件:
  = '值'          精确匹配
  LIKE '%关键词%'   模糊搜索
  > < >= <=      大小比较
  AND / OR       组合条件
  ORDER BY ASC   升序 / DESC 降序
  LIMIT 5        只取前5条
""")
