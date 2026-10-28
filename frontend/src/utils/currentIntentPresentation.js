/** Presentation helpers for the authoritative current-effective-intent API.
 *
 * The API has already selected one card per committed managed scope. This
 * module deliberately does not infer identity from prose, timestamps, or
 * Intent IDs: client-side de-duplication would hide an ownership defect.
 */
import { lifecyclePresentation } from './lifecyclePresentation.js'

const groupKeys = ['processing', 'enforcing', 'attention']
const bucketGroup = {
  ABNORMAL_WAITING: 'attention',
  EXECUTING: 'processing',
  ACTIVE: 'enforcing',
}

export function currentIntentGroups(snapshot = {}) {
  const projected = Object.fromEntries(groupKeys.map((key) => [key, []]))
  // The backend is the only semantic status resolver.  The client merely
  // places each authoritative display_bucket in its matching visual section;
  // it does not fall back to lifecycle, enforcement, or legacy group fields.
  for (const item of Array.isArray(snapshot.items) ? snapshot.items : []) {
    // `display_bucket` is authoritative for the current-intents projection.
    // A lifecycle fallback exists only for older API records; WITHDRAWN stays
    // out of the active projection even if a stale backend payload omits it.
    const group = bucketGroup[item?.display_bucket]
      || lifecyclePresentation(item?.lifecycle || item?.status).group
    if (group && projected[group]) projected[group].push(item)
  }
  return projected
}

export function currentEffectiveCount(snapshot = {}) {
  return Object.values(currentIntentGroups(snapshot)).reduce((count, items) => count + items.length, 0)
}
