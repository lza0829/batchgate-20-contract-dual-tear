<script setup>
import { computed, onMounted, ref } from 'vue'
import { deleteJSON, getJSON, putJSON } from '../api'

const contracts = ref([])
const jobs = ref([])
const err = ref('')
const msg = ref('')

const form = ref({ job_id: null, upstream_job_id: null, min_rows: 1, required_field: 'watermark_ts' })

const downstreamJobs = computed(() => jobs.value.filter(j => j.depends_on.length > 0))
const upstreamOptions = computed(() => {
  const job = jobs.value.find(j => j.id === form.value.job_id)
  if (!job) return []
  return jobs.value.filter(j => job.depends_on.includes(j.name))
})

async function refresh() {
  contracts.value = await getJSON('/api/contracts')
  jobs.value = await getJSON('/api/jobs')
}

async function save() {
  err.value = ''; msg.value = ''
  try {
    await putJSON(`/api/jobs/${form.value.job_id}/contracts/${form.value.upstream_job_id}`, {
      min_rows: Number(form.value.min_rows) || 0,
      required_field: (form.value.required_field || '').trim() || null,
    })
    msg.value = '契约已保存'
    await refresh()
  } catch (e) { err.value = String(e.message || e) }
}

async function remove(c) {
  err.value = ''; msg.value = ''
  try {
    await deleteJSON(`/api/jobs/${c.job_id}/contracts/${c.upstream_job_id}`)
    msg.value = `已删除 ${c.job_name} ← ${c.upstream_name} 的契约`
    await refresh()
  } catch (e) { err.value = String(e.message || e) }
}

onMounted(async () => {
  try {
    await refresh()
    if (downstreamJobs.value.length) form.value.job_id = downstreamJobs.value[0].id
    if (upstreamOptions.value.length) form.value.upstream_job_id = upstreamOptions.value[0].id
  } catch (e) { err.value = String(e.message || e) }
})
</script>

<template>
  <div class="page">
    <h1>产出契约</h1>
    <p class="muted">
      下游对指定上游的产出契约：上游尝试虽 success，但行数低于 min_rows、
      或产出摘要缺少 required_field 时，下游不执行，记 skipped_contract，且不抬自己的水位。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <p v-if="msg" class="tag ok">{{ msg }}</p>

    <div class="panel">
      <h3>配置契约</h3>
      <p>
        <label>下游作业
          <select v-model.number="form.job_id">
            <option v-for="j in downstreamJobs" :key="j.id" :value="j.id">{{ j.name }}</option>
          </select>
        </label>
        <label style="margin-left:0.75rem">上游作业
          <select v-model.number="form.upstream_job_id">
            <option v-for="j in upstreamOptions" :key="j.id" :value="j.id">{{ j.name }}</option>
          </select>
        </label>
      </p>
      <p>
        <label>min_rows
          <input type="number" min="0" v-model.number="form.min_rows" style="width:6rem" />
        </label>
        <label style="margin-left:0.75rem">required_field
          <input v-model="form.required_field" placeholder="如 watermark_ts，可留空" />
        </label>
        <button style="margin-left:0.75rem" :disabled="!form.job_id || !form.upstream_job_id" @click="save">保存（同对覆盖）</button>
      </p>
      <p class="muted">required_field 针对上游尝试详情（产出摘要），如 watermark_ts / artifact_rows / sample；留空则不校验字段。</p>
    </div>

    <div class="panel">
      <h3>已配置契约（{{ contracts.length }}）</h3>
      <table>
        <thead><tr><th>下游作业</th><th>上游作业</th><th>min_rows</th><th>required_field</th><th></th></tr></thead>
        <tbody>
          <tr v-for="c in contracts" :key="c.id">
            <td class="mono">{{ c.job_name }}</td>
            <td class="mono">{{ c.upstream_name }}</td>
            <td>{{ c.min_rows }}</td>
            <td class="mono">{{ c.required_field || '—' }}</td>
            <td><button class="secondary" @click="remove(c)">删除</button></td>
          </tr>
          <tr v-if="!contracts.length"><td colspan="5" class="muted">暂无契约</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
