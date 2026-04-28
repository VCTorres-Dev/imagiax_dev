const path = require('path');

module.exports = {
  mode: 'production',
  entry: path.join(__dirname, '../src/index.tsx'),
  output: {
    path: path.join(__dirname, '../dist'),
    filename: 'ohif-extension-imagiax-ai.umd.js',
    library: {
      type: 'umd',
      name: 'imagiaxOhifExtension',
    },
    globalObject: 'this',
    clean: true,
  },
  externals: {
    react: {
      commonjs: 'react',
      commonjs2: 'react',
      amd: 'React',
      root: 'React',
    },
    'react-dom': {
      commonjs: 'react-dom',
      commonjs2: 'react-dom',
      amd: 'ReactDOM',
      root: 'ReactDOM',
    },
    '@ohif/core': '@ohif/core',
    '@ohif/ui': '@ohif/ui',
  },
  resolve: {
    extensions: ['.tsx', '.ts', '.js', '.json'],
  },
  module: {
    rules: [
      {
        test: /\.(ts|tsx)$/,
        exclude: /node_modules/,
        use: 'babel-loader',
      },
      {
        test: /\.(png|jpg|jpeg|gif|svg)$/i,
        type: 'asset/inline',
      },
    ],
  },
  optimization: {
    minimize: true,
  },
};
