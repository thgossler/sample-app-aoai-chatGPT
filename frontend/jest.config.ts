import type { Config } from 'jest'

const config: Config = {
  verbose: true,
  transform: {
    '^.+\\.tsx?$': 'ts-jest'
  },
  setupFilesAfterEnv: ['<rootDir>/polyfills.js']
}

export default config
