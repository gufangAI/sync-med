# 语料台账 · 每一条都是一次真实误杀的产物

**删任何一条之前先读它这一行。** 这些不是"举例",是判据四轮改动里每一次踩过的坑。
前三轮的语料都留在临时目录、会话一结束就没了,下一轮又从头构造 —— 这个台账就是为了断掉那个循环。

## 存放约定

| 目录 | 什么 |
|---|---|
| `real/` | 线上真实 OCR 产出(从 R2 `_ocr/` 取回) |
| `synthetic/` | 构造语料 |
| `degeneracy/` | **书级**判据(`ocr_degeneracy.py`)的语料。与上面两个目录分开放,因为判的东西不同:上面是「这一页 OCR 得对不对」,这里是「这一整本有没有崩」,长度门槛(200 汉字起)与度量口径都不一样。`test_ocr_quality.py` 的语料完整性用例只扫 `real/` 与 `synthetic/`,这里由 `test_ocr_degeneracy.py` 自己那条同名用例管。 |

文件名后缀 `.esc.txt` = 内容以 `unicode_escape` 存,读的时候要解码(`tests/test_ocr_quality.py::load`)。
两种情况用它:① 线上真实原文不以明文进 public 仓(沿用本仓既有约定,见
`ocr_recover_scan.SNIPPET_MODE="escaped"`);② 控制符 / 替换符这类样本本来就没法当明文存。
构造出来的正常语料一律明文,可读性优先。

## real/

| 文件 | 哪一次事故 | 关键数 |
|---|---|---|
| `bcgmj21_page_0007.esc.txt` | 2026-07-27 抽样审计 199 页,**唯一一条 reject 就是它** —— 线上正货被误杀的第一份实证。清洗后 28 字的本草药名著录页(`_ocr/bcgmj21/page_0007.txt`)。它逼出了第一轮的"短页长度下限"。 | n=28, max_run=0.46(p=12) |

## synthetic/ — 正货(必须放行)

| 文件 | 哪一次事故 | 关键数 |
|---|---|---|
| `formula_guizhitang_26.txt` | **第一轮**。26 字桂枝湯组成被 `max_run` 判退。方剂组成页 = 药方库与 AI 寻脉「古籍出处」的原始来源,平台最核心的燃料。 | n=26, max_run=0.54 ≥ 0.45 → **承重**:靠 `MIN_LEN_FOR_REPEAT` 活着 |
| `formula_bazhentang_39.txt` | **第二轮**。39 字八珍湯,`repeat_ngram=0.54`。刚好卡在 40 字门槛下沿。 | n=39, rep=0.54 ≥ 0.50 → **承重** |
| `formula_shiquandabu_49.txt` | **第三轮**。40 字那条线把方剂组成页劈成两半,短的救了、长的照样被误杀;49 字十全大補湯就是长的那半。 | n=49, rep=0.49 —— 差 0.01 够不着判死门槛,**它本身不是承重哨兵**,留着是因为它是事故原页 |
| `formula_shiquandabu_50.txt` | **第三轮承重版**。同一事故,取 50 字使 `repeat_ngram=0.54` 真够着门槛,这样"背靠背豁免"一没就会红。 | n=50, rep=0.54, abs_repeat=1 → **承重**:靠 `scattered` 豁免活着 |
| `chengwuji_dense_commentary.txt` | 成無己《註解傷寒論》。血证本体在 `ocr_degeneracy.py`:字种下限取 60(一个猜出来的数)把 01-0022912 整本扔了,理由是"51 字种/千字";`FALLBACK_BASELINE` 的 `distinct=300` 又差点原样再犯一次。**那是书级指标、另一个模块**;这里钉的是同一个教训的页级版本 —— 字种高度集中的真古籍注文页不许被当成复读。实测本页 541 字 / 123 字种(227 per 1000),不是 51,别拿这个数当"51 的复现"。 | n=541, rep=0.04, 最高频字「者」×34 |
| `shanghan_body_long.txt` | 普通古籍正文长页。**零改动基准** —— 任何一轮改判据都不许动它的判决。 | n=86, 全部指标远离门槛 |
| `mulu_dotted_leader.txt` | **第三轮**。目录点线页(`…` 占 53%),`_REPEAT_MARKS` 这张表当初就是为它建的。 | n=57, mark_ratio=0.53 |
| `wakoku_ditto_marks.txt` | **第四轮**。和刻本同上记号(`〃ゝ々` 占 53%)。第四轮之前是 `suspect`,被 `garbage~0.35` 挂着 —— 一页正常的和刻本表格页天天被日审计成"异常"。 | n=34, mark_ratio=0.53, 旧 garbage=0.35 |
| `illegible_real_style_47.txt` | **第四轮主角**。47 字漫漶缺字页,`□` 占 64% → 旧 `garbage=0.64` + `single_char=0.64` 双杀。背靠背豁免早就生效了(`scattered_repeat=1`),但拦不住这两条。 | n=47, mark_ratio=0.64, content_len=17 |
| `illegible_real_style_preface.txt` | **第四轮**。序跋页零星漫漶,`□` 只占 14% —— 少量记号的页本来就该 `clean`,当"没过度反应"的基准。 | n=64, mark_ratio=0.14, reasons=clean |
| `illegible_boxes_r40/50/60/70.txt` | **第四轮**。`□` 占比 0.40 / 0.50 / 0.60 / 0.70 的漫漶页。改动前实测:占比 ≥0.50 一律 reject、≥0.30 一律 suspect,**与页长和糊法完全无关**(判死的是 `garbage_ratio` 这个逐字符计数)。 | n=120 各一条 |
| `illegible_boxes_scattered_r60.txt` | **第四轮**。同样 60%,但 `□` 逐字散布而不是成段糊。糊法不该影响判决。 | n=90, run_len=1 |
| `illegible_boxes_column_r60.txt` | **第四轮**。同样 60%,`□` 成整列糊在一起(真实漫漶最常见的形状),200 字长页。 | n=200, run_len=12 |
| `rare_kangxi_radical_index.txt` | **第五轮**。2026-07-28 CJK 覆盖表画窄,古籍生僻字被当乱码。字書/類書的部首檢字页,整页由康熙部首(U+2F00-2FDF)+ 汉字部首补充(U+2E80-2EFF)构成 —— 这两段全在旧表之外。改前 `garbage=0.95` 整页判死,还要再挨一条 `cjk_low=0.05`。 | n=84, 改前 garbage=0.95 → **承重**(本批占比最高的一条) |
| `rare_extb_variant_table.txt` | **第五轮**。扩展B(U+20000+)異體字對照页,按字書「一字數體」1 正 : 3 異 排。異體字正是校勘最需要精确保留的东西,而它们大多落在扩展B 以上。改前 `garbage=0.71` 判死。 | n=85, 改前 garbage=0.71 → **承重** |
| `rare_compat_wakoku_names.txt` | **第五轮**。和刻本正俗字對照页,俗字取兼容表意区 U+FA10 段(塚 﨑 﨓 﨔 凞 猪 益 礼 神 祥 福 靖 精 羽 …),那一段正是日本人名地名用字,内閣文庫和刻本漢方書满篇都是。改前是 `suspect`,被 `garbage~0.46` 挂着 —— 一页正常的校勘页天天被日审计成「异常」,本仓明令禁止的另一种虚惊。 | n=69, 改前 garbage=0.46(**suspect 档,不是 reject 档**,如实记) |
| `rare_hentaigana_wakoku.txt` | **第五轮**。和刻本假名旁訓,用的是変体仮名(Kana Supplement U+1B000-1B0FF,**星平面**)+ 片假名语音扩展。旧表只画了 BMP 里的现代平/片假名两段。这条同时是星平面(4 字节字符)那条路径的实测样本。改前 `garbage=0.54` 判死。 | n=96, 改前 garbage=0.54 → **承重** |

## synthetic/ — 垃圾(必须判死)

第三列 = **当年真正按住它的那条判据**。测试不只断言 `label`,还断言 `reasons` 里有这条 ——
只看 label 的话,某条判据悄悄失效时会被别的判据兜住,测试就成了永远绿的假哨兵。

| 文件 | 哪一次事故 | 该由谁抓 |
|---|---|---|
| `repeat_yingniao_x12.txt` | **第二轮** run 30339783256:全量扫 86 个历史判退页,11 页因新加的 40 字下限从 reject 翻成 ok,「鶯鳥」连出 12 次(24 字)是其中之一。 | `abs_repeat` |
| `repeat_egaku_x9.txt` | 同上:「鵝鶚」连出 9 次(18 字)。 | `abs_repeat` |
| `repeat_shang_x8.txt` | 同上:「上」连出 8 次(8 字)。**`ABS_REPEAT_REJECT=8` 就是取的这条实测最小恶性值**,不向下外推 —— 3..7 那一段至今一条样本都没有。 | `abs_repeat` |
| `repeat_unit20_x8.txt` | **第三轮**。20 字单元连出 8 次;旧 `ABS_REPEAT_MAX_UNIT=4` 数不到它,旧 `abs_repeat` 报 1 —— 长单元复读同时骗过判据和肉眼。 | `abs_repeat`(u=20) |
| `repeat_inline_in_body.txt` | **第三轮自报的代价②**。长页正文里夹 30 字局部复读:占比才 0.26,三条比例判据都够不着,次数够得着。 | `abs_repeat`(=15) |
| `line_dup_6lines.txt` | 整行复读 6 行。**背靠背豁免只许纠 `repeat_ngram`/`max_run`,绝不许纠到 `line_dup` 头上** —— 方剂组成页每行药名都不同,不会被 line_dup 冤枉。 | `line_dup` |
| `model_meta_japanese.txt` | 线上实测:一整页只有「图中包含的文字内容是日文。」13 字。这不是识别结果,是模型在说话;它进燃料池,SueAI 就会把它当古籍原文引用。重复类判据一条都抓不到(它一个字都不重复)。 | `model_meta` |
| `garbage_replacement.esc.txt` | 替换符乱码页(U+FFFD)。**第四轮放松 `garbage_ratio` 之后,它必须一点没松。** | `garbage` |
| `garbage_control_block.esc.txt` | 控制符块。同上。 | `garbage` |
| `garbage_ascii_spam.txt` | 纯 ASCII 符号刷屏。`garbage` 看不见它(符号都在 `_ASCII_OK` 里),靠重复判据按住 —— 这条防的是"以为 garbage 万能"。 | `repeat_ngram` |
| `marks_all_boxes.txt` | **第四轮**。整页只有 `□`,一个字都没读出来。 | `layout_mark_page` |
| `marks_kana_iteration_spam.txt` | **第四轮自己开出来的洞**。`ヽ` 落在 `_CJK_RE` 的假名区,一页纯 `ヽ` 的 `cjk_ratio=1.00`,同时骗过 `cjk_low` 和背靠背豁免两道闸;改动前唯一按住它的就是 `single_char=1.00`。只放松不补判据 9,它当场变 `ok`。`ゝ・ーヾゞ` 六个同款。 | `layout_mark_page` |
| `marks_zero_content.txt` | **第四轮自己开出来的洞之二**。`ヽ`×50 + `。`×10:记号占 83% 够不着死线 0.85,但记号之外**一个内容字都没有**。判据 9 的第二个分句专挡它。 | `layout_mark_page` |
| `marks_launder_replacement.esc.txt` | **第四轮洗白试探**。一半替换符 + 一半 `□`:记号只该从乱码的分子里退出,不该把分子稀释掉。`garbage` 仍须 ≥ 0.50。 | `garbage` |
| `rare_launder_replacement.esc.txt` | **第五轮洗白试探**。一半扩展B + 一半替换符:覆盖表放宽只该把生僻字从乱码的分子里拿出去,不该把分子稀释掉。`garbage` 改前 1.00、改后正好 0.50,仍够着判死门槛。 | `garbage` |
| `rare_spam_extb_x12.txt` | **第五轮的【收紧侧】,单独登记**。同一个扩展B字背靠背连出 12 次。`abs_repeat` 靠 `_unit_has_content` 决定"这段复读算不算数",覆盖表窄的时候单元里一个「内容字」都找不到,`abs_repeat` 恒报 1 —— 判据 7 对它**完全瞎**,改前这一页是 `ok`,直接进燃料池。它和 `repeat_shang_x8.txt`(「上」连出 8 次)是同一种垃圾,只是换了个判据不认识的字。**这是本轮唯一变严的方向,是同一覆盖缺口的垃圾侧,不是新增误杀。** | `abs_repeat`(=12) |

## degeneracy/ — 书级判据(`ocr_degeneracy.py`)

这一档 2026-07-28 之前**一条测试都没有**,而它才是本仓最贵那次误杀的本体:
成無己《註解傷寒論》01-0022912 被一个猜出来的字种下限 60 整本扔掉。
页级判错丢一页,书级判错丢**一整本**。

### 正货(必须通过,`verdict()` 返回空)

| 文件 | 哪一次事故 | 关键数 |
|---|---|---|
| `chengwuji_51_distinct_per_1000.txt` | **血证本体**。成無己《註解傷寒論》式注文,字种 **50.5/千字** —— 当年 `DISTINCT_ABS_MIN=60`(一个猜出来的数、outvote 掉了实测推出的 34)就是拿这个数把整本扔了;`FALLBACK_BASELINE` 的 `distinct=300` 又会算出门槛 90,在基线读不出来那条路径上把同一个 bug 原样再犯一次。**注意长度:古文词汇会饱和,同一本书取样越短 distinct/千字 越高**,拿 1000 字的短样本量出来是 105/千字,当年的 60 根本不会踩雷 —— 那就是个永远绿的假哨兵,所以这条语料必须 >2000 字。 | chars=2080, distinct=50.5, top1=0.043, rep=2 |
| `bencao_dose_column_runs.txt` | `REP_MAX` 当年是 12,咬过本草的剂量列 —— 密排药典的剂量栏里同一个字**合法地**成列连出。这条把连出做到 22(旧值判死、新值放行)。 | chars=1082, rep=22, distinct=101.7 |

### 垃圾(必须判退)

第三列 = **该抓它的那条判据**。每条语料只许踩**一条**判据(`test_each_junk_fixture_trips_exactly_one_criterion` 钉着)——
同时踩两条的语料测不出任何单条判据是否还活着,那正是假哨兵的来路。

| 文件 | 哪一次事故 | 该由谁抓 |
|---|---|---|
| `collapsed_low_distinct.txt` | 字种塌到 **21/千字** —— 本仓实测到的真退化值(而正常古文 50~60 很寻常,那正是 60 那个门槛的错处)。字符轮着来,`rep=1`、`top1` 压低,不让别的判据兜底。 | `字种过少` |
| `collapsed_char_run_81.txt` | `REP_MAX` 注释里那个实测抽样页:**一段 81 个相同字符**。总量控制住,`top1` 不许够到 0.15。 | `连续重复` |
| `collapsed_top1_domination.txt` | 单字**散布**霸屏(不连出)—— 连出判据抓不到它,只能靠 `top1`。 | `单字霸屏` |
| `collapsed_kana_page.txt` | 填满旧 `tcm-rag-768` 索引的两种失败模式之一:整页回来的是假名。 | `非汉字过多` |
| `collapsed_digit_page.txt` | 另一种:图版/索引扫描回来的整页数字。 | `非汉字过多` |
| `short_colophon_under_min.txt` | 不是垃圾,是**不给判决**那一档:`MIN_CHARS=200` 以下 `profile()` 返回 `None`。60 字的牌记能量出任何字种数,拿它下结论等于掷骰子 —— 与 `ocr_quality.MIN_LEN_FOR_REPEAT` 同一个思路。 | (`profile()` 返回 `None`) |

## 加新语料的规矩

1. 先问"它是哪一次事故的产物"。答不出 = 别加(举例性质的语料只会让下一轮的人不敢删)。
2. **必须能证伪**:加完先拿改动前的代码跑一遍,确认它是红的。红不了的语料是假哨兵。
   **"改动前的代码"是指真正的修复前 commit,不是 `git show HEAD:`** —— 第五轮踩过这个坑:
   HEAD 早已含修复,拿它当"改动前"跑,新语料全绿,证伪等于没做。
   第五轮的做法留在这当模板:①把修复前 commit 的 `ocr_quality.py` 原样取出来配新测试跑一遍;
   ②再做一遍只把改动点(覆盖表)退回旧值、其余不动的版本,并**逐码点比对**确认退回后的
   行为与旧 commit 完全一致(第五轮实测 0 个码点不一致),这样红的原因才锁死在改动点上。
3. 正货除了 `label`,把当年判死它的那个度量也钉住;垃圾把该抓它的判据名钉住。
4. 在本表里补一行,写清事故与关键数。
