import { expect, test } from '@playwright/test'

/**
 * 下沉分发批次优化 E2E：
 * - 批量选择多条规则整组一次提交，逐条保留成功/失败结果
 * - 批次结果保留成功键、失败键与回退范围，坏键不再整表回退
 * - 中断恢复只回退失败键界定的未完成范围，成功键不重复写入
 * - 切换页面后仍能看到批次结果；物化查询页核对刷新版本，关联记录同步呈现
 */
test('下沉分发批次：整组提交、恢复、单次提交与物化刷新版本核对', async ({ page }) => {
  test.setTimeout(120_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`)
  })

  // 1. 进入下沉分发页，清掉历史持久化数据后重新加载
  await page.goto('/sink_dispatch')
  await page.evaluate(() => localStorage.clear())
  await page.reload()
  await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(3)
  await page.getByTestId('select-row-1').check()
  await page.getByTestId('select-row-2').check()
  await expect(page.getByText('已选 2 条规则')).toBeVisible()

  // 全选/取消全选
  await page.getByTestId('select-all').check()
  await expect(page.locator('tbody input[type="checkbox"]:checked')).toHaveCount(3)
  await page.getByTestId('select-all').uncheck()
  await page.getByTestId('select-row-1').check()
  await page.getByTestId('select-row-2').check()

  // 2. 配置坏键、冲突做法、模拟中断，整组一次提交
  await page.getByTestId('batch-bad-keys').fill('K002,K005')
  await page.getByTestId('batch-policy').selectOption('advance_new_key_first')
  await page.getByTestId('batch-interrupted').check()
  await page.getByTestId('submit-batch').click()

  // 批次结果面板出现两条批次（逐条结果），且不做整表回退
  const batchRows = page.locator('[data-testid^="batch-row-"]')
  await expect(batchRows).toHaveCount(2)
  const firstRow = batchRows.first()
  await expect(firstRow).toContainText('否') // 整表回退列：否
  await expect(firstRow).toContainText('待恢复')

  // 3. 打开明细：成功键、失败键、回退范围、版本历史与来源记录同步呈现
  await firstRow.click()
  const detail = page.getByTestId('batch-detail')
  await expect(detail).toBeVisible()
  await expect(detail.locator('.key-list.ok li').first()).toBeVisible()
  await expect(detail.locator('.key-list.fail li').first()).toBeVisible()
  await expect(detail.locator('.range-list li').first()).toBeVisible()
  await expect(detail).toContainText('来源：sink_dispatch')

  const firstBatchId = (await firstRow.getAttribute('data-testid'))?.replace('batch-row-', '') ?? ''
  expect(firstBatchId).toMatch(/^BATCH-\d+-\d+$/)
  const attemptCell = page.locator(`[data-testid="batch-row-${firstBatchId}"] td`).nth(4)
  const attemptBefore = Number((await attemptCell.innerText()).trim())

  // 4. 已提交键台账：成功键只提交一次
  await page.getByTestId('load-committed').click()
  const committedPanel = page.getByTestId('committed-panel')
  await expect(committedPanel).toBeVisible()
  const committedKeys = await committedPanel.locator('tbody tr td:first-child').allInnerTexts()
  expect(committedKeys.length).toBe(new Set(committedKeys).size)

  // 5. 恢复分发：同窗口新键到达，坏键优先做法
  await page.getByTestId(`recover-${firstBatchId}`).click()
  const recoverModal = page.getByTestId('recover-modal')
  await expect(recoverModal).toBeVisible()
  await page.getByTestId('recover-new-keys').fill('NEW-001,NEW-002')
  await page.getByTestId('recover-policy').selectOption('retry_bad_key_first')
  await page.getByTestId('confirm-recover').click()
  await expect(recoverModal).toHaveCount(0)
  // 恢复后该批次尝试次数 +1，版本历史新增一条
  await expect(attemptCell).toContainText(String(attemptBefore + 1))

  // 6. 切换到物化查询页再切回：批次结果仍在（Pinia + 服务端双重保留）
  await page.goto('/materialized_view')
  await expect(page.getByTestId('refresh-panel')).toBeVisible()
  await page.goto('/sink_dispatch')
  await expect(page.locator('[data-testid^="batch-row-"]')).toHaveCount(2)
  await expect(page.getByTestId('batch-detail')).toBeVisible()

  // 7. 物化查询页核对刷新版本：选择与规则1湖表匹配的视图 MATE-0001
  await page.goto('/materialized_view')
  await page.getByTestId('confirm-mv').selectOption('1')
  await page.getByTestId('confirm-batch').selectOption(firstBatchId)
  await page.getByTestId('confirm-refresh-version').click()
  const versionRow = page.locator('[data-testid^="version-row-"]').first()
  await expect(versionRow).toBeVisible()
  await expect(versionRow).toContainText('MV-MATE-0001-R')
  await expect(versionRow).toContainText(firstBatchId)
  await expect(versionRow).toContainText('materialized_view')

  // 8. 再次切回下沉分发页，关联记录仍然可见
  await page.goto('/sink_dispatch')
  await expect(page.locator('[data-testid^="batch-row-"]')).toHaveCount(2)

  expect(browserErrors).toEqual([])
})
