# libgltf 案例研究：一个包的 12 年生死全程

**验证日期**：2026-10-28 ~ 2026-10-29（UDD、popcon、SWH、sources.debian.org、邮件列表归档实测）

libgltf 是 spring 项目的完美标本：既是"诈尸误报"的教训，也是"该埋未埋"的典型，还完整展示了人、公司、发行版的兴衰如何写进一个包的历史。

## 1. 基本信息（UDD 实测）

| 项 | 值 |
|---|---|
| 源码包 | libgltf 0.1.0-3（sid 仍在） |
| 二进制 | libgltf-0.1-1, libgltf-dev（`bin = 'libgltf-dev, libgltf-0.1-1'`，注意逗号列表陷阱） |
| 维护者 | Debian QA Group（孤儿） |
| 描述 | "Library for rendering glTF models" |
| Homepage 字段 | 空（孤儿烂相） |
| 存活套件 | bullseye / bookworm / trixie / forky / sid 全在 |
| WNPP | O: #889677（**2018-02-05 开，与最后一次打包同日**） |
| popcon | 45 装机 / 0-1 真实使用（2026-10） |
| RM 工单 | **从未有人发过**（bugs 表零记录） |

## 2. glTF vs libgltf：格式赢了，库死了

- **glTF**：Khronos 的 "3D 界 JPEG"（.gltf/.glb），2017 年 2.0 至今，Web 3D/AR/Blender 全在用，如日中天
- **libgltf**：LibreOffice 阵营的 C++ 渲染小库，用于在办公文档里渲染嵌入的 3D 模型，2014 年合同产物，2017 年被作者弃坑

## 3. 人物链（版权文件 + LinkedIn + 邮件列表交叉验证）

```
debian/copyright（原文，注意 2914 typo）:
  Copyright: (C) 2014 Zolnai Tamás <tamas.zolnai@collabora.com>
             (C) 2914 Yang Zhang <yangzhang@multicorewareinc.com>
  Source: https://gerrit.libreoffice.org/gitweb?p=libgltf.git;a=summary
```

- **Yang Zhang**（MulticoreWare 长春，2011-2019，SE/项目经理）：初始实现者。MulticoreWare = x265 背后的公司，这段代码大概率是合同外包的打底
- **Zolnai Tamás**（Collabora 布达佩斯）：2013 GSoC 入行，2013-14 实习期工作就是"集成第三方库用 OpenGL 渲染 3D 模型"（=libgltf）；2016-2021 转 Collabora Online；**2026-09 刚被 TDF 聘为 LibreOffice Online 开发者**——人很活跃，只是 8 年前弃了这个坑
- **Rene Engelhard**（Debian LibreOffice 御用打包工）：12 次上传全是她/他（2014-08-20 ~ 2018-02-05）

## 4. 死亡链条时间线

| 时间 | 事件 |
|---|---|
| 2013-14 | MulticoreWare（Yang Zhang）打底 + Collabora（Tamás）集成进 LibreOffice OpenGL/3D 计划 |
| 2014-08-20 | Rene 打包 0.0.0-1 进 Debian（为满足 LibreOffice 构建依赖） |
| 2017-10-27 | **死亡证明（作者亲笔）**：Red Hat 的 Caolán McNamara 问"3D 模型功能还要吗"，Tamás 回复 *"It seems a good idea to remove this 3D model support. It's not really used. Also libgltf is abandoned now (I stopped working on it for some time past)."*（listarchives.libreoffice.org/global/dev/2017/msg02289.html） |
| 2018-01-31 | popcon 峰值：**11,717 台装机 / 3 台真实使用**（stretch 的 LibreOffice 5.x 运行时依赖拖上车的） |
| 2018-02-05 | 最后上传 0.1.0-3 + **同日**开 O: 工单 #889677（弃养精确到天） |
| 2019 → 2026 | popcon 长尾衰减 11.7k → 862 → 433 → 140 → 79 → 45（机器升级甩包袱，年衰减 ~20%） |
| 2026 | QA 坟场躺尸第 8 年；watch 还在误报"新版本 0.1.0.gz" |

## 5. 诈尸误报教训

UDD 显示 upstream_version = **`0.1.0.gz`**——版本号里带 `.gz` 是 watch 文件 regex 把 tarball 文件名后缀粘进了版本号（`SELECT watch_file FROM upstream WHERE source='libgltf' AND release='sid'` 可眼见为实）。uscan 一比对"0.1.0.gz 比 0.1.0 新"→ 误报诈尸。

**规则**：版本号出现 `.gz/.tgz/.zip` 等后缀、或与已知版本模式不符 → 标记为 watch 假阳性，剔除告警。真实信号长这样：`xtermcontrol 3.8→3.11`、`aspell-ga 0.50→4.5`。

## 6. 为什么八年没人埋（auto-decrufter 射程外）

DAK 的 auto-decrufter 只埋"构建关系断裂"的包（NBS/NVIU）。libgltf 源码至今健康地构建出二进制——它是"活着但无用"，不在机器人射程内，**必须人工 RM**。这正是 spring 的价值：检测"待埋候选"→ 提示人发 RM。

该埋三条件（全中，实测）：
1. 零 build-rdep：`SELECT ... WHERE build_depends ILIKE '%libgltf%'` → 0 行
2. 零运行时 rdep：只有 libgltf-dev → libgltf-0.1-1 自引用（多架构重复 8 行）
3. 上游官方判死：作者 2017 弃坑信 + 唯一下游（LibreOffice）已移除 3D 功能 + popcon≈0

## 7. 记忆冗余验证（"光辉历史怎么办"）

删包 ≠ 焚书。libgltf 的历史现存于：

| 层 | 证据（实测 2026-10-28） |
|---|---|
| 上游 git | copyright 里的 gerrit 死链实为 301 → `https://git.libreoffice.org/libgltf/`（200，代码本体还在 TDA 服务器） |
| Software Heritage | origin 搜索：`deb://Debian/packages/libgltf` **358 次访问**（最后 2026-04-30）+ Ubuntu/PLD/solus 打包史各若干 |
| snapshot.debian.org | 2005 年起全时点档案，随时可拉回 .deb |
| archive.debian.org | bullseye/bookworm/trixie 冻结副本跟发行版活到 EOL 后永久保存 |
| 数据层 | UDD 12 次上传记录、O: #889677、popcon 12 年日粒度曲线（`qa.debian.org/cgi-bin/popcon-data`）、弃坑信在邮件列表归档 |

**哲学**：代码永生，死的是人对它的兴趣。spring 盯的不是尸体在哪，而是"谁开始挖了"——SWH 每次重访问做快照 diff，本身就是免费的"有人动过"传感器。

## 8. popcon 曲线解读（搭车者 vs 真实兴趣）

- **libgltf-0.1-1（运行库）**：2017-01 仅 21 台 → stretch 扩散 → 2018 峰值 11,717 台 → LO 6.x 弃用后雪崩 → 45 台长尾。`no_files` 占 95%+ = "装了但一个文件都没碰过"，99.97% 搭车率
- **libgltf-dev（开发包）**：峰值仅 100 台（活跃 ~15）——这才是真实全球开发者基数。运行库曲线是开发包的 117 倍，全是泡沫

结论：判断"诈尸后的接养价值"，popcon 时间序列是最强旁证——一条 12 年曲线能证明这个包**从来没被人类需要过**。

## 9. 附：埋葬模板（待执行）

```
To: submit@bugs.debian.org
Subject: RM: libgltf -- RoQA; orphaned since 2018 (#889677), upstream abandoned
 by author (2017-10, LibreOffice dev ML), zero build-rdeps, zero runtime
 rdeps, popcon 45/0
User: ftp.debian.org@packages.debian.org
Usertags: remove
```
