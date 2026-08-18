import { cloneDeep } from 'lodash'

import { AskResponse, Citation } from '../../api'

export type ParsedAnswer = {
  citations: Citation[]
  markdownFormatText: string,
  generated_chart: string | null
} | null

export const enumerateCitations = (citations: Citation[]) => {
  const filepathMap = new Map()
  for (const citation of citations) {
    const { filepath } = citation
    if (!filepath) {
      if (citation.part_index == 0) {
        citation.part_index = 1
      }
      continue
    }
    let part_i = 1
    if (filepathMap.has(filepath)) {
      part_i = filepathMap.get(filepath) + 1
    }
    filepathMap.set(filepath, part_i)
    citation.part_index = part_i
  }
  return citations
}

const getEmbeddedMetadata = (citation: Citation, field: string) => {
  const match = citation.content?.match(new RegExp(`^${field}\\s*:\\s*(.+)$`, 'm'))
  return match?.[1].trim() || ''
}

const normalizeCitationUrl = (url: string) => {
  try {
    const parsedUrl = new URL(url)
    parsedUrl.hash = ''
    parsedUrl.pathname = parsedUrl.pathname.replace(/__\d+(?=\.md$)/i, '')
    return parsedUrl.toString().replace(/\/$/, '')
  } catch {
    return url.replace(/#.*$/, '').replace(/__\d+(?=\.md$)/i, '')
  }
}

const getCitationKey = (citation: Citation, citationIndex: string) => {
  const sourceFile = getEmbeddedMetadata(citation, 'source_file')
  if (sourceFile) {
    return `file:${sourceFile.replace(/__\d+(?=\.md$)/i, '')}`
  }

  const sourceUrl = getEmbeddedMetadata(citation, 'source_url') || citation.url || ''
  if (sourceUrl) {
    return `url:${normalizeCitationUrl(sourceUrl)}`
  }

  if (citation.filepath) {
    return `file:${citation.filepath.replace(/__\d+(?=\.md$)/i, '')}`
  }

  return `id:${citationIndex}`
}

export function parseAnswer(answer: AskResponse): ParsedAnswer {
  if (typeof answer.answer !== "string") return null
  let answerText = answer.answer

  let lengthDocN = '[doc'.length
  let citationLinks = answerText.match(/\[(doc\d\d?\d?)]/g)

  if (!citationLinks) {
    citationLinks = answerText.match(/\[\d\d?\d?]/g)
    lengthDocN = '['.length
  }
  let filteredCitations = [] as Citation[]
  let citationReindex = 0
  const citationKeyMap = new Map<string, number>()
  citationLinks?.forEach(link => {
    // Replacing the links/citations with a number
    const citationIndex = link.slice(lengthDocN, link.length - 1)
    const citation = cloneDeep(answer.citations[Number(citationIndex) - 1]) as Citation
    if (!citation) return

    const citationKey = getCitationKey(citation, citationIndex)
    const existingReference = citationKeyMap.get(citationKey)
    if (existingReference) {
      answerText = answerText.replaceAll(link, ` ^${existingReference}^ `)
      return
    }

    const referenceNumber = ++citationReindex
    citationKeyMap.set(citationKey, referenceNumber)
    answerText = answerText.replaceAll(link, ` ^${referenceNumber}^ `)
    citation.id = citationIndex // original doc index to de-dupe
    citation.reindex_id = referenceNumber.toString() // reindex from 1 for display
    filteredCitations.push(citation)
  })

  filteredCitations = enumerateCitations(filteredCitations)

  return {
    citations: filteredCitations,
    markdownFormatText: answerText,
    generated_chart: answer.generated_chart
  }
}
