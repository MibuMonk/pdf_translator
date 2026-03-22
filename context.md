# 翻译背景与风格指南

## 适用场景
本工具用于翻译自动驾驶公司 Momenta 面向日系车厂（Honda 等）的技术培训材料（PPT/PDF）。

## 日文风格要求
- 使用正式技术文体，避免口语化
- 标题简洁有力，不超过原文字数的 1.5 倍
- 专业术语优先使用业界通用的片假名或汉字，不要生造词
- 保留英文缩写（AD、AI、LiDAR、BEV、EBM 等），不要强行音译
- 列表项保持简洁，使用名词性结尾（～化、～性、～機能）

## 绝对不翻译的词（原样保留英文）

### 公司与品牌（词本身保留，句子其他部分正常翻译）
- Momenta
- Honda
- 例：「Honda Toolchain Workshop」→「Honda ツールチェーン ワークショップ」（Honda 保留，其他词翻译）

### Momenta 自有产品与平台名
Momenta Box / Mbox / MB、Mviz、FDI Cloud、FCC、FDC、CDI、CFDI、DFDI、
ETP、TMP、ETI、VVP、EBM、Model Lab、Data Lake、Data SPA、MFbag、DPI、FST、
DDOD、DDLD、DLP

### 版本与型号
R6、R7、R9、AD Algorithm 2.0 / 3.0 / 4.0 / 5.0

---

## 缩略语全称对照

### 系统与平台
| 缩略语 | 全称 | 日文说明 |
|--------|------|---------|
| CLA | Closed-Loop Automation | クローズドループ自動化 |
| CDI | Cloud Data Infra | クラウドデータ基盤 |
| FDI | Fleet Data Infra | フリートデータ基盤 |
| CFDI | Customer Fleet Data Infra | 量産車データ収集基盤 |
| DFDI | Dev Fleet Data Infra | 開発車データ収集基盤 |
| ETP | Event Tagging Platform | イベントタグ付けプラットフォーム |
| TMP | Training Management Platform | 学習管理プラットフォーム |
| ETI | EBM Training and Inference | EBM学習・推論基盤 |
| VVP | Vehicle Verification Platform | 車両検証プラットフォーム |
| DPI | Data Production Line Infra | データ生産ライン基盤 |
| FCC | Fleet Config Cloud | フリート設定クラウド |
| FDC | Fleet Data Capture | フリートデータキャプチャ |

### モデル・アルゴリズム
| 缩略语 | 全称 | 日文说明 |
|--------|------|---------|
| EBM | End-to-End Big Model | エンドツーエンド大規模モデル |
| DDOD | Data-Driven Object & Obstacle Detection | データ駆動型物体・障害物検出 |
| DDLD | Data-Driven Landmark Detection | データ駆動型ランドマーク検出 |
| DLP | Deep Learning Planning | 深層学習プランニング |
| AD | Autonomous Driving | 自動運転 |
| RL | Reinforcement Learning | 強化学習 |
| FST | Function Scenario Tree | 機能シナリオツリー |

### テスト・リリース
| 缩略语 | 全称 | 日文说明 |
|--------|------|---------|
| CI | Continuous Integration | 継続的インテグレーション |
| CT | Continuous Testing | 継続的テスト |
| CD | Continuous Deployment | 継続的デプロイメント |
| RCT | Algorithm Release CT | アルゴリズムリリース継続的テスト |
| Sim ICT | Simulation Integration CT | シミュレーション統合継続的テスト |

### KPI・評価指標
| 缩略语 | 全称 | 日文说明 |
|--------|------|---------|
| MPD | Miles per Disengagement | 解除間隔距離 |
| MPI | Miles per Issue | 課題間隔距離 |
| CPD | Counts per Disengagement | 解除あたり回数 |
| KPI | Key Performance Indicator | 重要業績評価指標 |
| SOP | Start of Production | 量産開始 |

### ハードウェア・車両
| 缩略语 | 全称 | 日文说明 |
|--------|------|---------|
| SoC | System on Chip | システムオンチップ |
| ADCU | Autonomous Driving Control Unit | 自動運転制御ユニット |
| IMU | Inertial Measurement Unit | 慣性計測装置 |
| LiDAR | Light Detection and Ranging | ライダー |
| OTA | Over-the-Air | OTA無線更新 |
| VRU | Vulnerable Road User | 交通弱者 |
| OEM | Original Equipment Manufacturer | 完成車メーカー |
| FOV | Field of View | 視野角 |

---

## 技术术语推荐译法

### 核心技术
| English | 日本語 |
|---------|--------|
| Autonomous Driving | 自動運転 |
| End-to-End | エンドツーエンド（E2E） |
| Closed-Loop Automation | クローズドループ自動化 |
| Data-Driven | データ駆動型 |
| World Model | ワールドモデル |
| Foundation Model | 基盤モデル |
| Reinforcement Learning | 強化学習 |
| Imitation Learning | 模倣学習 |
| Reward | 報酬 |
| Perception | 認識 |
| Planning | プランニング |
| Prediction | 予測 |
| Localization | 自己位置推定 |
| Sensor Fusion | センサーフュージョン |
| Trajectory | 軌跡 |
| Ground Truth | 正解データ |
| Inference | 推論 |
| Scalable / Scalability | スケーラブル / スケーラビリティ |
| Mass Production | 量産 |

### 数据与工程
| English | 日本語 |
|---------|--------|
| Data Pipeline | データパイプライン |
| Golden Data | 高品質教師データ |
| Corner Case | コーナーケース |
| Fleet | 車両群 |
| Calibration | キャリブレーション |
| Point Cloud | 点群 |
| Bounding Box | バウンディングボックス |
| Disengagement | システム解除 |
| Takeover | ドライバー引き継ぎ |
| Simulation | シミュレーション |

### 驾驶场景
| English | 日本語 |
|---------|--------|
| Lane Change | 車線変更 |
| Unprotected Left Turn | 非保護左折 |
| Intersection | 交差点 |
| Highway | 高速道路 |
| Urban / City Road | 市街地 |
| Narrow Road | 狭路 |
| Cut-in | 割り込み |
| Pedestrian | 歩行者 |
| Ego Vehicle | 自車 |
| Good Behavior | 良い挙動 |
| Bad Behavior | 悪い挙動 |
| Good（图例ラベル単独） | 良い |
| Bad（图例ラベル単独） | 悪い |

---

## 人名（固定変換）

以下の人名は必ず指定の漢字表記を使用すること。ローマ字・カタカナ表記は不可。

| ローマ字 | 正式表記 |
|----------|---------|
| Shikama / shikama | 四竃 |
| Nagashima / nagashima | 長島 |
