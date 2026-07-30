# 礼品卡预订单前端改动说明

## 一、改动范围

本次前端只需要调整以下两个环节：

1. 购物车创建预订单时，`predictOrder` 请求增加礼品卡 ID 和礼品卡协议状态。
2. 订单试算时，接收 `calculateOrder` 响应新增的礼品卡 ID。

`submitOrder` 前端不需要改动。该接口的 `giftCardSelected`、`giftCardId` 均为已有字段，后端本次只增加礼品卡 ID 一致性校验。

## 二、购物车创建预订单

### 1. 接口

- PC：`POST /api/pc/rs-order/rsOrder/predictOrder`
- 小程序：`POST /api/mini/rs-order/rsOrder/predictOrder`

### 2. 新增请求字段

| 字段 | 类型 | 是否必填 | 说明 |
| --- | --- | --- | --- |
| `giftCardId` | `Long` | 否 | 当前选择的礼品卡 ID；未选择礼品卡时传 `null` 或不传 |
| `giftCardAgreement` | `Boolean` | 否 | 是否同意礼品卡协议；选择礼品卡时必须为 `true` |

购物车创建预订单时，`source` 继续传 `2`。

请求字段示例：

```json
{
  "source": 2,
  "giftCardId": 10001,
  "giftCardAgreement": true
}
```

以上只展示本次新增和相关字段，商品列表等原有参数保持不变。

### 3. 页面交互

#### 已选择礼品卡

当 `giftCardId` 不为空时，用户点击“去结算”需要检查礼品卡协议是否已经勾选：

- 已勾选：传 `giftCardAgreement=true`，正常创建预订单。
- 未勾选：不进入结算页，并提示：

```text
您还未接受礼品卡协议，无法使用礼品卡进行下单！
```

前端可以在调用接口前进行校验；后端也会进行相同校验，前端按现有错误提示方式展示后端返回的提示语即可。

#### 未选择礼品卡

当 `giftCardId` 为空时，不校验礼品卡协议：

- `giftCardId` 传 `null` 或不传。
- `giftCardAgreement` 可以传 `null`、`false` 或不传。
- 正常创建预订单并进入结算页。

## 三、订单试算

### 1. 接口

- PC：`POST /api/pc/rs-order/rsOrder/calculateOrder`
- 小程序：`POST /api/mini/rs-order/rsOrder/calculateOrder`

### 2. 请求

请求参数不变，继续传现有的 `randomKey`、收货地址等字段，不需要新增礼品卡参数。

### 3. 响应

响应的 `data` 中新增：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `giftCardId` | `Long` | 创建预订单时保存的礼品卡 ID，可以为空 |

响应增量示例：

```json
{
  "data": {
    "giftCardId": 10001
  }
}
```

前端使用该字段确认本次结算对应的礼品卡：

- `giftCardId` 不为空：按该 ID 回显本次预订单关联的礼品卡。
- `giftCardId` 为空：按未选择礼品卡处理。

## 四、提交订单

`submitOrder` 前端无需改动，继续使用现有请求参数和现有提交逻辑。

后端本次仅增加校验：当前端原有请求中的 `giftCardId` 不为空时，校验其是否与预订单保存的礼品卡 ID 一致。该校验不涉及新的前端字段。

## 五、前端联调检查

1. 未选择礼品卡时，可以正常创建预订单并进入结算页。
2. 选择礼品卡但未同意协议时，显示指定提示语，不进入结算页。
3. 选择礼品卡并同意协议时，`predictOrder` 正常返回 `randomKey`。
4. `calculateOrder` 返回与创建预订单时一致的 `giftCardId`。
5. `submitOrder` 请求和前端现有提交逻辑保持不变。
