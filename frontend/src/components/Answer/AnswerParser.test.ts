import { cloneDeep } from 'lodash'

import { AskResponse, Citation } from '../../api' // Ensure this path matches the location of your types

import { enumerateCitations, parseAnswer, ParsedAnswer } from './AnswerParser' // Update the path accordingly

const sampleCitations: Citation[] = [
  {
    id: 'doc1',
    filepath: 'file1.pdf',
    part_index: undefined,
    content: '',
    title: null,
    url: null,
    metadata: null,
    chunk_id: null,
    reindex_id: null
  },
  {
    id: 'doc2',
    filepath: 'file1.pdf',
    part_index: undefined,
    content: '',
    title: null,
    url: null,
    metadata: null,
    chunk_id: null,
    reindex_id: null
  },
  {
    id: 'doc3',
    filepath: 'file2.pdf',
    part_index: undefined,
    content: '',
    title: null,
    url: null,
    metadata: null,
    chunk_id: null,
    reindex_id: null
  }
]

const sampleAnswer: AskResponse = {
  answer: 'This is an example answer with citations [doc1] and [doc2].',
  citations: cloneDeep(sampleCitations),
  generated_chart: null
}

describe('enumerateCitations', () => {
  it('assigns unique part_index based on filepath', () => {
    const results = enumerateCitations(cloneDeep(sampleCitations))
    expect(results[0].part_index).toEqual(1)
    expect(results[1].part_index).toEqual(2)
    expect(results[2].part_index).toEqual(1)
  })
})

describe('parseAnswer', () => {
  it('deduplicates citations that resolve to the same URL', () => {
    const answer = {
      ...sampleAnswer,
      answer: 'The same document is cited [doc1] and [doc2].',
      citations: sampleCitations.map((citation, index) => ({
        ...citation,
        url: index < 2 ? 'https://docs.example.com/kubernetes' : citation.url
      }))
    }

    const parsed = parseAnswer(answer)

    expect(parsed?.citations).toHaveLength(1)
    expect(parsed?.markdownFormatText).toBe('The same document is cited  ^1^  and  ^1^ .')
  })

  it('deduplicates chunk URLs and fragments for one source document', () => {
    const answer: AskResponse = {
      answer: 'See [doc1] and [doc2].',
      citations: [
        { ...sampleCitations[0], url: 'https://docs.example.com/guide__001.md#part-a' },
        { ...sampleCitations[1], url: 'https://docs.example.com/guide__002.md#part-b' }
      ],
      generated_chart: null
    }

    const parsed = parseAnswer(answer)

    expect(parsed?.citations).toHaveLength(1)
    expect(parsed?.markdownFormatText).toBe('See  ^1^  and  ^1^ .')
  })

  it('uses source_file metadata as the citation identity', () => {
    const answer: AskResponse = {
      answer: 'See [doc1] and [doc2].',
      citations: [
        {
          ...sampleCitations[0],
          content: 'source_file: /docs/guide.md\nContent 1',
          url: 'https://storage.example.com/guide__001.md'
        },
        {
          ...sampleCitations[1],
          content: 'source_file: /docs/guide.md\nContent 2',
          url: 'https://storage.example.com/guide__002.md'
        }
      ],
      generated_chart: null
    }

    const parsed = parseAnswer(answer)

    expect(parsed?.citations).toHaveLength(1)
  })
})
