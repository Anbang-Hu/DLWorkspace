import { groupBy } from 'lodash'

export type Job = any

const ACTIVE_STATUSES: { [status: string]: string } = {
  unapproved: 'Unapproved',
  queued: 'Pending',
  scheduling: 'Pending',
  running: 'Running',
  pausing: 'Paused',
  paused: 'Paused'
}

export const isStatusActive = (job: Job) => {
  return job['jobStatus'] in ACTIVE_STATUSES
}

export const groupByActiveStatus = (jobs: Job[]) => {
  return groupBy(jobs, (job) => {
    return ACTIVE_STATUSES[job['jobStatus']] || 'Inactive'
  })
}
