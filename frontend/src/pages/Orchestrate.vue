<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'
const history = ref([])
const last = ref(null)
const busy = ref(false)
const err = ref('')
async function refresh() { history.value = await getJSON('/api/orchestrations') }
async function runOnce() {
  err.value = ''; busy.value = true
  try {
    last.value = await postJSON('/api/orchestrations')
    await refresh()
  } catch (e) { err.value = String(e.message || e) }
  finally { busy.value = false }
}
onMounted(async () => {
  try {
    await refresh()
    // Show the newest run's report on load, so a contract failure stays visible
    // after navigation/reload instead of only right after clicking 执行编排.
    if (!last.value && history.value.length) last.value = history.value[0]
  } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>编排台</h1>
    <p class="muted">触发一次全图编排：按依赖层执行；上游未成功记 skipped_dep；上游成功但产出契约不过记 skipped_contract。</p>
    <div class="panel">
      <button :disabled="busy" @click="runOnce">{{ busy ? '编排中…' : '执行编排' }}</button>
    </div>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel" v-if="last">
      <h3>最近一次报告</h3>
      <p>
        #{{ last.id }} · <span class="tag" :class="last.status === 'ok' ? 'ok' : 'warn'">{{ last.status }}</span> ·
        succeeded {{ last.report?.succeeded }} /
        failed {{ last.report?.failed }} /
        skipped_dep {{ last.report?.skipped_dep }} /
        skipped_contract {{ last.report?.skipped_contract }} /
        attempts {{ last.report?.attempt_count }}
      </p>
      <p class="mono muted">watermarked_jobs={{ last.report?.watermarked_jobs }} layers={{ last.report?.layer_count }}</p>
      <p v-if="last.report?.reconciliation">
        互证：
        <span class="tag" :class="last.report.reconciliation.ok ? 'ok' : 'danger'">
          {{ last.report.reconciliation.ok ? '尝试/水位/契约结论/跳过数一致' : '存在不一致' }}
        </span>
        <span class="mono muted" style="margin-left:0.5rem">
          尝试计数 {{ last.report.reconciliation.attempt_counts_match ? '✓' : '✗' }} ·
          契约跳过数 {{ last.report.reconciliation.skipped_contract_matches ? '✓' : '✗' }} ·
          跳过合计 {{ last.report.reconciliation.skipped_total_matches ? '✓' : '✗' }} ·
          拦截作业未被信任 {{ last.report.reconciliation.blocked_not_trusted ? '✓' : '✗' }} ·
          拦截作业水位未动 {{ last.report.reconciliation.blocked_watermarks_untouched ? '✓' : '✗' }} ·
          已执行水位与尝试一致 {{ last.report.reconciliation.executed_watermarks_match ? '✓' : '✗' }}
        </span>
      </p>
      <div v-if="last.report?.contract_checks?.length">
        <h4>契约核验</h4>
        <table>
          <thead><tr><th>下游作业</th><th>核验上游</th><th>契约结论</th><th>是否执行</th><th>下游水位</th></tr></thead>
          <tbody>
            <tr v-for="c in last.report.contract_checks" :key="c.job">
              <td class="mono">{{ c.job }}</td>
              <td class="mono">{{ c.upstreams.join(', ') }}</td>
              <td><span class="tag" :class="c.passed ? 'ok' : 'danger'">{{ c.passed ? '通过' : '不通过' }}</span></td>
              <td>{{ c.executed ? '已执行' : '已拦截' }}</td>
              <td>
                <span v-if="!c.executed" class="tag warn">未前进</span>
                <span v-else-if="c.watermark_advanced" class="tag ok">已前进</span>
                <span v-else class="tag warn">未前进</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="last.report?.contract_blocked?.length">
        <h4>契约拦截（下游未执行，水位未前进）</h4>
        <p v-for="b in last.report.contract_blocked" :key="b.job" class="mono">
          <span class="tag danger">{{ b.job }}</span>
          <span v-for="(v, i) in b.violations" :key="i"> · {{ v.message }}</span>
        </p>
      </div>
      <p>
        <router-link :to="{ path: '/attempts', query: { orchestration_id: last.id } }">查看本次尝试 →</router-link>
        ·
        <router-link to="/watermarks">产出水位 →</router-link>
        ·
        <router-link to="/contracts">产出契约 →</router-link>
      </p>
    </div>
    <div class="panel">
      <h3>历史编排</h3>
      <table>
        <thead><tr><th>ID</th><th>状态</th><th>成功</th><th>失败</th><th>依赖跳过</th><th>契约跳过</th><th>开始</th></tr></thead>
        <tbody>
          <tr v-for="o in history" :key="o.id">
            <td>{{ o.id }}</td>
            <td>{{ o.status }}</td>
            <td>{{ o.report?.succeeded }}</td>
            <td>{{ o.report?.failed }}</td>
            <td>{{ o.report?.skipped_dep ?? '—' }}</td>
            <td>{{ o.report?.skipped_contract ?? '—' }}</td>
            <td class="mono">{{ o.started_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
